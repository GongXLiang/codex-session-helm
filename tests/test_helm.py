"""Behavior checks for dependency gates, scheduling, and read-only output."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import helm


def task(task_id: str, **changes: object) -> dict[str, object]:
    """Make a small, valid raw task that a case can override."""
    result: dict[str, object] = {
        "id": task_id, "title": task_id, "version": 1, "status": "planned",
        "depends_on": [], "workspace": "host:/project", "write_paths": [task_id],
    }
    result.update(changes)
    return result


def state(*tasks: dict[str, object], limit: int = 2) -> dict[str, object]:
    """Make a raw workflow with explicit path comparison semantics."""
    return {
        "schema_version": 1, "workflow_id": "test", "updated_at": "2026-11-11T00:00:00Z",
        "concurrency_limit": limit, "case_sensitive_paths": False, "tasks": list(tasks),
    }


def accepted(task_id: str, **changes: object) -> dict[str, object]:
    """Make a declared acceptance, with evidence checked separately."""
    return task(task_id, **{"status": "accepted", "report": "report.md", "review": "review.md", **changes})


def dependency(task_id: str, version: int = 1) -> list[dict[str, object]]:
    """Pin one dependency version."""
    return [{"id": task_id, "version": version}]


class GraphTests(unittest.TestCase):
    def test_bad_graphs_are_rejected(self) -> None:
        cases = [
            state(task("A"), task("A")),
            state(task("A", depends_on=dependency("missing"))),
            state(task("A", depends_on=dependency("B")), task("B", depends_on=dependency("A"))),
            state(task("A", depends_on=dependency("A"))),
            state(task("A"), task("B", depends_on=dependency("A") * 2)),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(helm.StateError):
                helm.parse_workflow(raw)

    def test_required_types_and_scopes_are_checked(self) -> None:
        cases = [
            state(task("A", version=True)), state(task("A", write_paths=["../outside"])),
            state(task("A", write_paths=["C:\\outside"])), state(task("A", write_paths=["output/*"])),
            state(task("A", write_paths=["/outside"])), state(task("A", depends_on=None)),
            state(task("A", status="accepted")), state(task("A", status="paused")),
            {**state(task("A")), "updated_at": "2026-11-11"},
            {**state(task("A")), "case_sensitive_paths": "false"},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(helm.StateError):
                helm.parse_workflow(raw)

    def test_only_acceptance_opens_gate(self) -> None:
        for status in ("planned", "reported", "running", "waiting_input", "waiting_approval", "paused", "detached", "failed"):
            raw = state(task("A", status=status, reason="pending"), task("B", depends_on=dependency("A")))
            with self.subTest(status=status):
                self.assertNotIn("B", helm.analyze(helm.parse_workflow(raw)).batch)
        raw = state(accepted("A"), task("B", depends_on=dependency("A")))
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("B",))

    def test_new_version_invalidates_transitive_acceptance(self) -> None:
        raw = state(accepted("A", version=2), accepted("B", depends_on=dependency("A")),
                    accepted("C", depends_on=dependency("B")), task("D", depends_on=dependency("C")))
        result = helm.analyze(helm.parse_workflow(raw))
        self.assertEqual(result.stale, {"B", "C", "D"})
        self.assertEqual(result.batch, ())

    def test_withdrawn_acceptance_invalidates_descendants(self) -> None:
        raw = state(task("A", status="needs_revision", reason="input changed"),
                    accepted("B", depends_on=dependency("A")), task("C", depends_on=dependency("B")))
        result = helm.analyze(helm.parse_workflow(raw))
        self.assertEqual(result.stale, {"B", "C"})
        self.assertEqual(result.batch, ())

    def test_declared_active_process_holds_slot_even_when_paused(self) -> None:
        raw = state(task("A", status="paused", reason="turn stopped", run_state="active"), task("B"), limit=1)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ())

    def test_case_insensitive_parent_scopes_conflict(self) -> None:
        raw = state(task("A", status="running", write_paths=["Output"]),
                    task("B", write_paths=["output/result.md"]), task("C", write_paths=["other.md"]), limit=3)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("C",))

    def test_distinct_workspaces_and_readonly_tasks_can_run(self) -> None:
        raw = state(task("A", status="running", write_paths=["."]),
                    task("B", write_paths=[]), task("C", workspace="host:/other", write_paths=["."]), limit=3)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("B", "C"))

    def test_active_conflict_blocks_new_batch(self) -> None:
        raw = state(task("A", status="running", write_paths=["."]),
                    task("B", status="running"), task("C"), limit=3)
        result = helm.analyze(helm.parse_workflow(raw))
        self.assertEqual(result.conflicts, (("A", "B"),))
        self.assertEqual(result.batch, ())

    def test_batch_does_not_overlap_and_respects_capacity(self) -> None:
        raw = state(task("A", write_paths=["data"]), task("B", write_paths=["data/a"]), task("C"), task("D"), limit=2)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("A", "C"))

    def test_priority_then_descendants_controls_order(self) -> None:
        raw = state(task("A"), task("B"), task("C", depends_on=dependency("B")), limit=1)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("B",))
        raw = state(task("A", priority=1), task("B", priority=2), task("C", depends_on=dependency("B")), limit=1)
        self.assertEqual(helm.analyze(helm.parse_workflow(raw)).batch, ("A",))

    def test_scope_conflict_remains_visible_when_capacity_is_full(self) -> None:
        raw = state(task("F", status="paused", run_state="active", reason="pending check"),
                    task("D", write_paths=["Output"]), task("E", write_paths=["output/result.md"]), limit=2)
        result = helm.analyze(helm.parse_workflow(raw))
        self.assertEqual(result.batch, ("D",))
        self.assertIn("重叠", result.reasons["E"])
        self.assertIn("并发名额", result.reasons["E"])


class OutputTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        """Capture the public CLI result without touching external resources."""
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = helm.main(arguments)
        return code, output.getvalue(), error.getvalue()

    def test_checks_and_board_never_change_input(self) -> None:
        raw = state(task("A"))
        original = deepcopy(raw)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
            for command in ("check", "ready", "board"):
                self.assertEqual(self.run_cli(command, str(path))[0], 0)
            self.assertEqual(self.run_cli("board", str(path), "--output", str(path))[0], 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertEqual(raw, original)

    def test_exit_codes_and_evidence_existence(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state(accepted("A"))), encoding="utf-8")
            self.assertEqual(self.run_cli("check", str(path), "--check-files")[0], 2)
            for name in ("report.md", "review.md"):
                (path.parent / name).write_text("test evidence", encoding="utf-8")
            self.assertEqual(self.run_cli("check", str(path), "--check-files")[0], 0)
            raw = state(accepted("A", version=2), accepted("B", depends_on=dependency("A")))
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(self.run_cli("check", str(path))[0], 1)
            raw = state(task("A", status="running", write_paths=["."]), task("B", status="running"))
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(self.run_cli("ready", str(path))[0], 1)

    def test_board_escapes_labels_and_uses_generated_node_ids(self) -> None:
        raw = state(task('X"] --> Y\n```', title='<script>|"oops"\nnew line', write_paths=[]))
        workflow = helm.parse_workflow(raw)
        board = helm.render_board(workflow, helm.analyze(workflow))
        self.assertNotIn("<script>", board)
        self.assertNotIn('X"] --> Y', board)
        self.assertEqual(board.count("```"), 2)
        self.assertIn('N0["', board)
        self.assertIn("&#124;", board)

    def test_cli_uses_utf8_when_host_default_cannot_encode_chinese(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state(task("A", title="核对正文"))), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "helm.py"
            environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
            for command in ("check", "ready", "board"):
                with self.subTest(command=command):
                    result = subprocess.run([sys.executable, str(script), command, str(path)],
                                            env=environment, capture_output=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                    output = result.stdout.decode("utf-8")
                    if command == "board":
                        self.assertIn("核对正文", output)
                    else:
                        self.assertEqual(json.loads(output)["next_batch"], ["A"])

    def test_cli_error_paths_are_utf8_in_legacy_locale(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "缺失.json"
            script = Path(__file__).resolve().parents[1] / "scripts" / "helm.py"
            result = subprocess.run([sys.executable, str(script), "check", str(missing)],
                                    env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("缺失.json", result.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
