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


def packet(ledger: Ledger, db: sqlite3.Connection, project: str) -> dict[str, object]:
    from helm_store import LedgerError, encode, verify_evidence
    context = db.execute("SELECT * FROM contexts WHERE project=?", (project,)).fetchone()
    if context is None:
        raise LedgerError("project context is missing; save a sourced context capsule first")
    p = ledger._project(db, project)
    sources = json.loads(context["sources"])
    verify_evidence(Path(p["root"]), sources)
    tasks = [dict(r) for r in db.execute("SELECT workflow,id,revision,attempt,status,report,review,note FROM tasks WHERE project=? ORDER BY workflow,id", (project,))]
    checkpoint = db.execute("SELECT * FROM checkpoints WHERE project=? ORDER BY id DESC LIMIT 1", (project,)).fetchone()
    result: dict[str, object] = {"project": project, "title": p["title"], "root": p["root"],
        "context_version": context["version"], "brief": json.loads(context["brief"]), "sources": sources,
        "tasks": tasks, "checkpoint": dict(checkpoint) if checkpoint else None,
        "checkpoint_is_current": bool(checkpoint and checkpoint["context_version"] == context["version"]),
        "pending_decisions": [dict(r) for r in db.execute("SELECT * FROM decisions WHERE project=? AND state='open' ORDER BY created_at", (project,))],
        "recent_verdicts": [dict(r) for r in db.execute("SELECT task,revision,state,note,evidence FROM reviews WHERE project=? AND state IN ('accepted','needs_revision','waiting_input') ORDER BY closed_at DESC LIMIT 8", (project,))]}
    # Fingerprint authoritative inputs and task generations, not a checkpoint narrative.
    result["fingerprint"] = hashlib.sha256(encode({"context": dict(context), "tasks": tasks, "decisions": result["pending_decisions"]}).encode()).hexdigest()
    if len(encode(result)) > 24000:
        raise LedgerError("project packet exceeds 24000 characters; shorten the capsule or archive completed workflows before proceeding")
    return result


def require_focus(ledger: Ledger, db: sqlite3.Connection, project: str, owner: str, epoch: int) -> None:
    from helm_store import LedgerError
    if ledger._meta(db, "schema") == "1":
        return  # Existing v1 ledgers stay readable; explicit upgrade enables context gates.
    current = db.execute("SELECT * FROM focus WHERE slot=1").fetchone()
    if not current or (current["project"], current["owner"], current["epoch"]) != (project, owner, epoch):
        raise LedgerError("load this project's focus packet before dispatch, revision or verdict")
    if current["fingerprint"] != packet(ledger, db, project)["fingerprint"]:
        raise LedgerError("project context changed; reload focus before acting")


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
