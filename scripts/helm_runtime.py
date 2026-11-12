#!/usr/bin/env python3
"""Optional SessionHelm portfolio CLI; standard library only, local-first."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
import sys

from helm_store import Ledger, LedgerError, TaskKey, atomic_json, publish, read_json


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, help="Explicit local ledger path, outside the skill checkout")
    root.add_argument("--owner", help="Current supervisor thread ID; a workflow identifier, not authentication")
    root.add_argument("--epoch", type=int, help="Current supervisor handoff generation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--active-limit", type=int, default=3)
    init.add_argument("--review-limit", type=int, default=4)
    register = commands.add_parser("register")
    register.add_argument("config", type=Path)
    commands.add_parser("scan")
    commands.add_parser("portfolio")
    limits = commands.add_parser("limits")
    limits.add_argument("--active", type=int, required=True)
    limits.add_argument("--reviews", type=int, required=True)
    migrate = commands.add_parser("upgrade")
    migrate.add_argument("--backup", type=Path, required=True)
    capsule = commands.add_parser("save-context")
    capsule.add_argument("--project", required=True)
    capsule.add_argument("--capsule", type=Path, required=True)
    capsule.add_argument("--expected", type=int, required=True)
    focus = commands.add_parser("focus")
    focus.add_argument("--project", required=True)
    focus.add_argument("--output", type=Path)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--project", required=True)
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--next", required=True)
    checkpoint.add_argument("--question", action="append", default=[])
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output", type=Path)
    export = commands.add_parser("export")
    export.add_argument("--project", required=True)
    export.add_argument("--workflow", required=True)
    export.add_argument("--output", type=Path)
    for name in ("prepare", "context", "observe", "revise", "request-decision"):
        command = commands.add_parser(name)
        for field in ("project", "workflow", "task"):
            command.add_argument("--" + field, required=True)
        if name == "context":
            command.add_argument("--output", type=Path)
        if name == "observe":
            command.add_argument("--thread", required=True)
            command.add_argument("--state", required=True)
            command.add_argument("--evidence", required=True)
        if name == "revise":
            command.add_argument("--spec", type=Path, required=True)
            command.add_argument("--reason", required=True)
        if name == "request-decision":
            command.add_argument("--question", required=True)
            command.add_argument("--impact", required=True)
            command.add_argument("--option", action="append", required=True)
    decision = commands.add_parser("resolve-decision")
    decision.add_argument("--decision", required=True)
    decision.add_argument("--answer", required=True)
    decision.add_argument("--evidence", required=True)
    bind = commands.add_parser("bind")
    bind.add_argument("--action", required=True)
    bind.add_argument("--thread", required=True)
    bind.add_argument("--receipt", required=True)
    bind.add_argument("--output", type=Path, help="Save the executor's binding file")
    cancel = commands.add_parser("cancel-dispatch")
    cancel.add_argument("--action", required=True)
    cancel.add_argument("--evidence", required=True, help="Evidence that native dispatch did not occur, not merely a timeout")
    for name in ("claim", "release", "review"):
        command = commands.add_parser(name)
        command.add_argument("--review", required=name != "claim")
        if name != "claim":
            command.add_argument("--token", required=True)
        if name == "review":
            command.add_argument("--verdict", choices=["accepted", "needs_revision", "waiting_input"], required=True)
            command.add_argument("--evidence", required=True, help="Project-relative independent review file")
            command.add_argument("--note", required=True)
    handoff = commands.add_parser("handoff")
    handoff.add_argument("--to", required=True)
    handoff.add_argument("--reason", required=True)
    emit = commands.add_parser("publish")
    emit.add_argument("--root", type=Path, required=True)
    emit.add_argument("--binding", type=Path, required=True)
    emit.add_argument("--kind", choices=["STARTED", "MILESTONE", "RESULT", "BLOCKED", "NEEDS_INPUT", "NEEDS_APPROVAL", "FAILED"], required=True)
    emit.add_argument("--summary", required=True)
    emit.add_argument("--report")
    emit.add_argument("--artifact", action="append", default=[])
    emit.add_argument("--event-id")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--ready-file", type=Path)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result: object = {"ok": True}
        if args.command == "publish":
            result = {"event_file": str(publish(args.root, read_json(args.binding), args.kind, args.summary,
                args.report, args.artifact, args.event_id)), "next": "Send a short native report to the authorized supervisor."}
        else:
            if args.db is None:
                raise LedgerError("--db is required")
            ledger = Ledger(args.db)
            reads = {"scan", "snapshot", "context", "export", "serve", "portfolio"}
            if args.command not in reads and not args.owner:
                raise LedgerError("--owner is required for supervisor mutations")
            if args.command not in reads | {"init"} and args.epoch is None:
                raise LedgerError("--epoch is required; read it from snapshot")
            key: TaskKey = {"project": getattr(args, "project", ""), "workflow": getattr(args, "workflow", ""), "task": getattr(args, "task", "")}
            if args.command == "init":
                ledger.initialize(args.owner, args.active_limit, args.review_limit)
            elif args.command == "register":
                ledger.register(read_json(args.config), args.owner, args.epoch)
            elif args.command == "upgrade":
                ledger.upgrade(args.backup, args.owner, args.epoch)
            elif args.command == "save-context":
                result = {"context_version": ledger.save_context(args.project, read_json(args.capsule), args.expected, args.owner, args.epoch)}
            elif args.command == "focus":
                result = ledger.focus(args.project, args.owner, args.epoch)
            elif args.command == "checkpoint":
                ledger.checkpoint(args.project, args.summary, args.next, args.question, args.owner, args.epoch)
            elif args.command == "portfolio":
                result = ledger.portfolio()
            elif args.command == "limits":
                ledger.limits(args.active, args.reviews, args.owner, args.epoch)
            elif args.command == "request-decision":
                result = {"decision_id": ledger.request_decision(key, args.question, args.impact, args.option, args.owner, args.epoch)}
            elif args.command == "resolve-decision":
                ledger.resolve_decision(args.decision, args.answer, args.evidence, args.owner, args.epoch)
            elif args.command == "scan":
                result = ledger.scan()
            elif args.command == "snapshot":
                result = ledger.snapshot()
            elif args.command == "context":
                result = ledger.context(key)
            elif args.command == "export":
                result = ledger.export(args.project, args.workflow)
            elif args.command == "prepare":
                result = ledger.prepare(key, args.owner, args.epoch)
            elif args.command == "bind":
                result = ledger.bind(args.action, args.thread, args.receipt, args.owner, args.epoch)
            elif args.command == "observe":
                ledger.observe(key, args.thread, args.state, args.evidence, args.owner, args.epoch)
            elif args.command == "cancel-dispatch":
                ledger.cancel_dispatch(args.action, args.evidence, args.owner, args.epoch)
            elif args.command == "claim":
                result = ledger.claim(args.owner, args.epoch, args.review)
            elif args.command == "release":
                ledger.release(args.review, args.token, args.owner, args.epoch)
            elif args.command == "review":
                ledger.decide(args.review, args.token, args.verdict, args.evidence, args.note, args.owner, args.epoch)
            elif args.command == "revise":
                ledger.revise(key, read_json(args.spec), args.reason, args.owner, args.epoch)
            elif args.command == "handoff":
                result = {"epoch": ledger.handoff(args.to, args.owner, args.epoch, args.reason)}
            elif args.command == "serve":
                from helm_server import serve
                serve(ledger, args.port, args.ready_file)
                return 0
            output = getattr(args, "output", None)
            if output is not None:
                if output.resolve() == ledger.path:
                    raise LedgerError("cannot overwrite the ledger with an export")
                atomic_json(output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LedgerError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
