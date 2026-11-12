"""Bounded, project-local supervisor context. These packets do not erase chat history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helm_store import Ledger

CONTEXT_SCHEMA = """
CREATE TABLE contexts (project TEXT PRIMARY KEY REFERENCES projects(id), version INTEGER NOT NULL,
 brief TEXT NOT NULL, sources TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE focus (slot INTEGER PRIMARY KEY CHECK(slot=1), project TEXT NOT NULL,
 fingerprint TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL, loaded_at TEXT NOT NULL);
CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
 context_version INTEGER NOT NULL, summary TEXT NOT NULL, next_step TEXT NOT NULL,
 questions TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, project TEXT NOT NULL, workflow TEXT NOT NULL,
 task TEXT NOT NULL, revision INTEGER NOT NULL, question TEXT NOT NULL, impact TEXT NOT NULL,
 options TEXT NOT NULL, state TEXT NOT NULL, answer TEXT, evidence TEXT, created_at TEXT NOT NULL);
"""


def bounded_text(value: object, limit: int = 1200) -> str:
    from helm_store import LedgerError, text
    result = text(value)
    if len(result) > limit:
        raise LedgerError(f"context text exceeds {limit} characters; keep detail in source files")
    return result


def packet(ledger: Ledger, db: sqlite3.Connection, project: str, workflow: str | None = None,
           task: str | None = None) -> dict[str, object]:
    from helm_store import LedgerError, encode, verify_evidence
    context = db.execute("SELECT * FROM contexts WHERE project=?", (project,)).fetchone()
    if context is None:
        raise LedgerError("project context is missing; save a sourced context capsule first")
    p = ledger._project(db, project)
    sources = json.loads(context["sources"])
    verify_evidence(Path(p["root"]), sources)
    if bool(workflow) != bool(task):
        raise LedgerError("task focus requires both workflow and task")
    modern = ledger._meta(db, "schema") == "3"
    workflows = [dict(r) for r in db.execute("SELECT workflow,generation,state,summary FROM workflow_state WHERE project=? ORDER BY workflow", (project,))] if modern else []
    rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE project=? ORDER BY workflow,id", (project,))]
    if task:
        index = {(r['workflow'], r['id']): r for r in rows}
        selected: set[tuple[str, str]] = set()
        pending = [(workflow, task)]
        while pending:
            key = pending.pop()
            if key in selected:
                continue
            if key not in index:
                raise LedgerError("unknown focus target or dependency")
            selected.add(key)
            pending.extend((workflow, d['id']) for d in json.loads(index[key]['spec'])['depends_on'])
        relevant = [r for r in rows if (r['workflow'], r['id']) in selected]
    else:
        archived = {w['workflow'] for w in workflows if w['state'] == 'archived'}
        relevant = [r for r in rows if r['workflow'] not in archived]
    # The fingerprint includes every relevant generation, even when the visible list is paged.
    semantic = [{k: r[k] for k in ('workflow', 'id', 'revision', 'attempt', 'status', 'manifest')} for r in relevant]
    priority = {'reported': 0, 'waiting_input': 1, 'waiting_approval': 1, 'needs_revision': 2, 'failed': 2, 'running': 3, 'planned': 4, 'accepted': 5}
    visible = sorted(relevant, key=lambda r: (r['id'] != task if task else False, priority.get(r['status'], 4), r['workflow'], r['id']))[:12]
    tasks = [{**{k: r[k] for k in ('workflow','id','revision','attempt','status','report','review')}, 'note': r['note'][:240]} for r in visible]
    decisions = [dict(r) for r in db.execute("SELECT * FROM decisions WHERE project=? AND state='open' ORDER BY created_at", (project,))]
    if task:
        decisions = [d for d in decisions if (d['workflow'], d['task']) in selected]
    checkpoint = db.execute("SELECT * FROM checkpoints WHERE project=? ORDER BY id DESC LIMIT 1", (project,)).fetchone()
    result: dict[str, object] = {"project": project, "title": p["title"], "root": p["root"],
        "context_version": context["version"], "brief": json.loads(context["brief"]), "sources": sources,
        "scope": {"workflow": workflow, "task": task}, "tasks": tasks,
        "task_counts": {"relevant": len(relevant), "shown": len(tasks), "omitted": len(relevant)-len(tasks)},
        "workflows": [{**w, 'summary': w['summary'][:240]} for w in workflows[:12]],
        "workflow_count": len(workflows),
        "checkpoint": {"context_version": checkpoint['context_version'], "summary": checkpoint['summary'][:400], "next_step": checkpoint['next_step'][:400],
            "questions": [q[:200] for q in json.loads(checkpoint['questions'])[:4]], "question_count": len(json.loads(checkpoint['questions']))} if checkpoint else None,
        "checkpoint_is_current": bool(checkpoint and checkpoint["context_version"] == context["version"]),
        "pending_decisions": [{k: d[k] for k in ('id','workflow','task','revision','question','impact')} for d in decisions[:3]],
        "pending_decision_count": len(decisions),
        "recent_verdicts": [{**dict(r), 'note': (r['note'] or '')[:240]} for r in db.execute("SELECT workflow,task,revision,state,note FROM reviews WHERE project=? AND state IN ('accepted','needs_revision','waiting_input') ORDER BY closed_at DESC LIMIT 3", (project,))],
        "history": "Use list-tasks with offset and include-archived; read the selected task card and full report before acting."}
    # Fingerprint authoritative inputs and task generations, not a checkpoint narrative.
    scope_state = [w for w in workflows if not task or w['workflow'] == workflow]
    # A workflow append unrelated to the focused task does not change its inputs.
    if task:
        scope_state = [{k: w[k] for k in ('workflow','state')} for w in scope_state]
    result["fingerprint"] = hashlib.sha256(encode({"context": dict(context), "tasks": semantic, "decisions": decisions, "scope": result['scope'], "workflows": scope_state}).encode()).hexdigest()
    return result


def require_focus(ledger: Ledger, db: sqlite3.Connection, project: str, owner: str, epoch: int,
                  key: dict[str, str] | None = None) -> None:
    from helm_store import LedgerError
    if ledger._meta(db, "schema") == "1":
        return  # Existing v1 ledgers stay readable; explicit upgrade enables context gates.
    current = db.execute("SELECT * FROM focus WHERE slot=1").fetchone()
    if not current or (current["project"], current["owner"], current["epoch"]) != (project, owner, epoch):
        raise LedgerError("load this project's focus packet before dispatch, revision or verdict")
    scope = {"workflow": None, "task": None}
    if ledger._meta(db, "schema") == "3":
        view = db.execute('SELECT scope FROM context_views WHERE project=?', (project,)).fetchone()
        if view:
            scope = json.loads(view['scope'])
    if scope['task'] and (key is None or (key['workflow'], key['task']) != (scope['workflow'], scope['task'])):
        raise LedgerError("load project focus or the exact task focus before this action")
    if current["fingerprint"] != packet(ledger, db, project, **scope)["fingerprint"]:
        raise LedgerError("project context changed; reload focus before acting")


def delta_packet(previous: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    """A delta only refers to the same owner/epoch/scope; callers persist the full packet."""
    fields = {k: v for k, v in current.items() if k not in {'tasks','fingerprint','project','scope'} and previous.get(k) != v}
    old = {(t['workflow'], t['id']): t for t in previous['tasks']}
    new = {(t['workflow'], t['id']): t for t in current['tasks']}
    changed_tasks = [v for k, v in new.items() if old.get(k) != v]
    removed = [{'workflow': k[0], 'task': k[1]} for k in old.keys() - new.keys()]
    return {'project': current['project'], 'scope': current['scope'], 'fingerprint': current['fingerprint'],
        'mode': 'delta', 'changed': bool(fields or changed_tasks or removed), 'fields': fields,
        'tasks': changed_tasks, 'removed_from_view': removed}


def queue(ledger: Ledger, db: sqlite3.Connection) -> list[dict[str, object]]:
    from datetime import datetime, timezone
    from helm_store import obj
    current = datetime.now(timezone.utc)
    last = ledger._meta(db, "last_review_project")
    rows = [dict(r) for r in db.execute("SELECT * FROM reviews WHERE state IN ('queued','claimed')")]
    for row in rows:
        count = 0
        for child in db.execute("SELECT * FROM tasks WHERE project=? AND workflow=? AND status='planned'", (row["project"], row["workflow"])):
            deps = obj(json.loads(child["spec"]))["depends_on"]
            matches = [d for d in deps if d["id"] == row["task"] and d["version"] == row["revision"]]
            if not matches:
                continue
            others = [d for d in deps if d["id"] != row["task"]]
            if all((p := ledger._task(db, {"project": row["project"], "workflow": row["workflow"], "task": d["id"]}))["status"] == "accepted"
                   and p["revision"] == d["version"] and ledger._deps_valid(db, p) for d in others):
                count += 1
        row["unlocks"] = count
        row["wait_seconds"] = max(0, round((current - datetime.fromisoformat(row["created_at"])).total_seconds(), 1))
    # Aged items bypass rotation/impact and remain FIFO, so impact cannot starve them.
    rows.sort(key=lambda r: (r["state"] != "claimed", r["wait_seconds"] < 600,
        r["created_at"] if r["wait_seconds"] >= 600 else "",
        r["project"] == last, -r["unlocks"], r["created_at"], r["id"]))
    return rows
