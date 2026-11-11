#!/usr/bin/env python3
"""Read-only checks, batch suggestions, and Markdown boards for SessionHelm."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import html
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys


STATUSES = {
    "planned": "待派发", "running": "执行中", "reported": "待验收",
    "accepted": "已验收", "needs_revision": "待修订", "waiting_input": "待输入",
    "waiting_approval": "待审批", "failed": "失败", "paused": "暂停",
    "detached": "已解除关联",
}
RUN_STATES = {"unknown", "active", "idle", "notLoaded", "interrupted", "failed"}


class StateError(ValueError):
    """The supplied state is ambiguous or inconsistent."""


@dataclass(frozen=True)
class Dependency:
    task_id: str
    version: int


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    version: int
    status: str
    run_state: str
    dependencies: tuple[Dependency, ...]
    workspace: str
    writes: tuple[str, ...]
    priority: int
    reason: str
    report: str
    review: str


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    updated_at: str
    concurrency_limit: int
    case_sensitive_paths: bool
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Analysis:
    stale: frozenset[str]
    batch: tuple[str, ...]
    reasons: dict[str, str]
    conflicts: tuple[tuple[str, str], ...]
    active_count: int


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise StateError(f"{label}: expected an object with string keys")
    return dict(value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise StateError(f"{label}: expected a list")
    return list(value)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise StateError(f"{label}: expected {'a' if empty else 'a nonempty'} string")
    return value.strip()


def _integer(value: object, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise StateError(f"{label}: expected an integer >= {minimum}")
    return value


def _write_path(value: object) -> str:
    text = _text(value, "write_paths item").replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", text) or ".." in path.parts:
        raise StateError("write_paths must be relative paths inside their declared workspace")
    if any(mark in text for mark in ("*", "?", "[", "]")):
        raise StateError("write_paths must be concrete paths, not glob patterns")
    return str(path)


def parse_workflow(value: object) -> Workflow:
    """Parse a versioned dependency graph; no files or chats are changed."""
    raw = _object(value, "workflow")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise StateError("schema_version must be 1")
    updated = _text(raw.get("updated_at"), "updated_at")
    try:
        timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError("updated_at must be an ISO timestamp") from exc
    if timestamp.tzinfo is None:
        raise StateError("updated_at must include a timezone")
    case_sensitive = raw.get("case_sensitive_paths")
    if type(case_sensitive) is not bool:
        raise StateError("case_sensitive_paths must explicitly be true or false")
    tasks: list[Task] = []
    for task_value in _array(raw.get("tasks"), "tasks"):
        item = _object(task_value, "task")
        task_id = _text(item.get("id"), "task.id")
        dependencies: list[Dependency] = []
        for dependency_value in _array(item.get("depends_on"), f"{task_id}.depends_on"):
            dep = _object(dependency_value, "dependency")
            dependencies.append(Dependency(
                _text(dep.get("id"), "dependency.id"),
                _integer(dep.get("version"), "dependency.version"),
            ))
        if len({dep.task_id for dep in dependencies}) != len(dependencies):
            raise StateError(f"{task_id}: duplicate dependency")
        status = _text(item.get("status"), f"{task_id}.status")
        run_state = _text(item.get("run_state", "unknown"), "run_state")
        if status not in STATUSES or run_state not in RUN_STATES:
            raise StateError(f"{task_id}: unrecognized task or run state")
        writes = tuple(_write_path(p) for p in _array(item.get("write_paths"), "write_paths"))
        workspace = _text(item.get("workspace"), f"{task_id}.workspace")
        reason = _text(item.get("reason", ""), "reason", empty=True)
        report = _text(item.get("report", ""), "report", empty=True)
        review = _text(item.get("review", ""), "review", empty=True)
        if status == "accepted" and (not report or not review):
            raise StateError(f"{task_id}: accepted requires report and review paths")
        if status in {"waiting_input", "waiting_approval", "failed", "paused", "detached", "needs_revision"} and not reason:
            raise StateError(f"{task_id}: this status requires a reason")
        tasks.append(Task(
            task_id, _text(item.get("title"), "title"),
            _integer(item.get("version"), "version"), status, run_state,
            tuple(dependencies), workspace, writes,
            _integer(item.get("priority", 0), "priority", 0), reason, report, review,
        ))
    by_id = {task.task_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise StateError("duplicate task id")
    remaining = {task.task_id: {dep.task_id for dep in task.dependencies} for task in tasks}
    for task_id, deps in remaining.items():
        missing = deps - by_id.keys()
        if missing:
            raise StateError(f"{task_id}: missing dependencies {sorted(missing)}")
    while remaining:
        ready = {task_id for task_id, deps in remaining.items() if not deps}
        if not ready:
            raise StateError(f"dependency cycle involving {sorted(remaining)}")
        remaining = {task_id: deps - ready for task_id, deps in remaining.items() if task_id not in ready}
    return Workflow(
        _text(raw.get("workflow_id"), "workflow_id"), updated,
        _integer(raw.get("concurrency_limit"), "concurrency_limit"),
        case_sensitive, tuple(tasks),
    )


def load_workflow(path: Path) -> Workflow:
    """Read a UTF-8 JSON state file, optionally with a UTF-8 BOM."""
    raw: object = json.loads(path.read_text(encoding="utf-8-sig"))
    return parse_workflow(raw)


def writes_overlap(left: Task, right: Task, case_sensitive: bool) -> bool:
    """Compare declared scopes; actual symlinks and process state need host checks."""
    if left.workspace != right.workspace:
        return False
    for a in left.writes:
        for b in right.writes:
            a_parts = PurePosixPath(a if case_sensitive else a.casefold()).parts
            b_parts = PurePosixPath(b if case_sensitive else b.casefold()).parts
            if a_parts[:len(b_parts)] == b_parts or b_parts[:len(a_parts)] == a_parts:
                return True
    return False


def analyze(workflow: Workflow) -> Analysis:
    """Find stale dependency versions and a bounded, nonoverlapping next batch."""
    by_id = {task.task_id: task for task in workflow.tasks}
    stale = {
        task.task_id for task in workflow.tasks
        if any(
            dep.version != by_id[dep.task_id].version
            or (task.status == "accepted" and by_id[dep.task_id].status != "accepted")
            for dep in task.dependencies
        )
    }
    while True:
        expanded = stale | {
            task.task_id for task in workflow.tasks
            if any(dep.task_id in stale for dep in task.dependencies)
        }
        if expanded == stale:
            break
        stale = expanded
    active = [task for task in workflow.tasks if task.status == "running" or task.run_state == "active"]
    conflicts = tuple(
        (left.task_id, right.task_id)
        for i, left in enumerate(active) for right in active[i + 1:]
        if writes_overlap(left, right, workflow.case_sensitive_paths)
    )
    reasons: dict[str, str] = {}
    candidates: list[Task] = []
    for task in workflow.tasks:
        if task.task_id in stale:
            reasons[task.task_id] = "依赖版本或验收已失效，需复核后更新引用"
        elif task.status != "planned":
            reasons[task.task_id] = task.reason or STATUSES[task.status]
        elif task.run_state == "active":
            reasons[task.task_id] = "宿主仍在运行，不能再次派发"
        elif any(by_id[dep.task_id].status != "accepted" for dep in task.dependencies):
            reasons[task.task_id] = "前置任务尚未全部验收"
        else:
            candidates.append(task)
    descendants = {task.task_id: set() for task in workflow.tasks}
    for task in workflow.tasks:
        pending = [dep.task_id for dep in task.dependencies]
        visited: set[str] = set()
        while pending:
            ancestor = pending.pop()
            if ancestor in visited:
                continue
            visited.add(ancestor)
            descendants[ancestor].add(task.task_id)
            pending.extend(dep.task_id for dep in by_id[ancestor].dependencies)
    candidates.sort(key=lambda task: (task.priority, -len(descendants[task.task_id]), task.task_id))
    batch: list[Task] = []
    slots = max(0, workflow.concurrency_limit - len(active))
    for task in candidates:
        if conflicts:
            reasons[task.task_id] = "已有运行任务写入冲突，先处理冲突"
        elif any(writes_overlap(task, other, workflow.case_sensitive_paths) for other in active + batch):
            reasons[task.task_id] = "就绪，但写入范围与运行中或本批任务重叠" + (
                "；同时等待并发名额" if len(batch) >= slots else ""
            )
        elif len(batch) >= slots:
            reasons[task.task_id] = "就绪，等待并发名额"
        else:
            batch.append(task)
            reasons[task.task_id] = "本批可派发；仍需核对现场状态和授权"
    return Analysis(frozenset(stale), tuple(task.task_id for task in batch), reasons, conflicts, len(active))


def _cell(value: str) -> str:
    return html.escape(value, quote=True).replace("|", "&#124;").replace("`", "&#96;").replace("\r", " ").replace("\n", " ")


def render_board(workflow: Workflow, result: Analysis) -> str:
    """Create a derived Markdown table and Mermaid dependency diagram."""
    accepted = sum(task.status == "accepted" and task.task_id not in result.stale for task in workflow.tasks)
    lines = [
        f"# SessionHelm · {_cell(workflow.workflow_id)}", "",
        f"状态时间：{workflow.updated_at}。本页为派生视图，请在权威状态中更新事实。", "",
        f"有效验收 {accepted}/{len(workflow.tasks)} · 运行中 {result.active_count} · 待复核 {len(result.stale)}", "",
        "下一批建议：" + ("、".join(_cell(task_id) for task_id in result.batch) or "暂无") + "。", "",
        "| 任务 | 当前版本 | 任务状态 | 宿主状态 | 依赖版本 | 下一动作或阻塞 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for task in workflow.tasks:
        status = "待复核（依赖已失效）" if task.task_id in result.stale else STATUSES[task.status]
        deps = ", ".join(f"{dep.task_id}@{dep.version}" for dep in task.dependencies) or "—"
        cells = [f"{task.task_id} · {task.title}", str(task.version), status, task.run_state, deps, result.reasons[task.task_id]]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    if result.conflicts:
        lines.extend(["", "运行中写入冲突：" + "；".join(f"{_cell(a)} ↔ {_cell(b)}" for a, b in result.conflicts)])
    lines.extend(["", "```mermaid", "flowchart LR"])
    nodes = {task.task_id: f"N{index}" for index, task in enumerate(workflow.tasks)}
    for task in workflow.tasks:
        state = "待复核" if task.task_id in result.stale else STATUSES[task.status]
        label = _cell(f"{task.task_id} v{task.version} · {task.title} · {state}")
        lines.append(f'  {nodes[task.task_id]}["{label}"]')
        for dep in task.dependencies:
            lines.append(f'  {nodes[dep.task_id]} -->|v{dep.version}| {nodes[task.task_id]}')
    lines.extend(["```", "", "证据入口："])
    for task in workflow.tasks:
        if task.report or task.review:
            lines.append(f"- {_cell(task.task_id)}：报告 {_cell(task.report) or '—'}；验收 {_cell(task.review) or '—'}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; only an explicit --output writes a derived file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "ready", "board"))
    parser.add_argument("state", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-files", action="store_true", help="Check evidence paths relative to the state file")
    args = parser.parse_args(argv)
    state_path: Path = args.state
    output_path: Path | None = args.output
    try:
        workflow = load_workflow(state_path)
        result = analyze(workflow)
        if args.check_files:
            for task in workflow.tasks:
                for evidence in (task.report, task.review):
                    if evidence and not (state_path.parent / evidence).is_file():
                        raise StateError(f"{task.task_id}: evidence file missing: {evidence}")
        if output_path and args.command != "board":
            raise StateError("--output is only supported for board")
        if args.command == "board":
            content = render_board(workflow, result)
            if output_path:
                if output_path.resolve() == state_path.resolve() or (
                    output_path.exists() and output_path.samefile(state_path)
                ):
                    raise StateError("board output must not overwrite the authoritative state")
                output_path.write_text(content, encoding="utf-8", newline="\n")
            else:
                print(content, end="")
        else:
            print(json.dumps({
                "workflow_id": workflow.workflow_id,
                "next_batch": result.batch, "requires_review": sorted(result.stale),
                "active_write_conflicts": result.conflicts, "reasons": result.reasons,
                "note": "Read-only advice; no task dispatched or acceptance decision made.",
            }, ensure_ascii=False, indent=2))
        return 1 if result.conflicts or any(task.status == "accepted" and task.task_id in result.stale for task in workflow.tasks) else 0
    except (OSError, ValueError) as exc:
        print(f"SessionHelm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
