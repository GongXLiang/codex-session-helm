"""Local portfolio ledger. No desktop API calls, process control, or model calls."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import TypedDict
import uuid

import helm
import helm_context


class LedgerError(ValueError):
    """An operation conflicts with the recorded scope or current revision."""


class Evidence(TypedDict):
    path: str
    sha256: str


class TaskKey(TypedDict):
    project: str
    workflow: str
    task: str


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise LedgerError("expected an object")
    return dict(value)


def text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError("expected nonempty text")
    return value.strip()


def integer(value: object, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise LedgerError(f"expected integer >= {minimum}")
    return value


def identifier(value: object) -> str:
    value = text(value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        raise LedgerError("IDs must use 1-100 ASCII letters, digits, dot, underscore or hyphen")
    return value


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path: Path) -> dict[str, object]:
    return obj(json.loads(path.read_text(encoding="utf-8-sig")))


def scoped(root: Path, relative: object, *, exists: bool = False) -> Path:
    name = text(relative).replace("\\", "/")
    candidate = Path(name)
    if candidate.is_absolute() or ":" in name or ".." in candidate.parts:
        raise LedgerError("path must be relative to its registered project")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise LedgerError("path escapes project (including links or junctions)")
    if exists and not resolved.is_file():
        raise LedgerError(f"evidence is missing: {name}")
    return resolved


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def evidence(root: Path, path: object) -> Evidence:
    resolved = scoped(root, path, exists=True)
    return {"path": resolved.relative_to(root.resolve()).as_posix(), "sha256": digest(resolved)}


def verify_evidence(root: Path, manifest: object) -> list[Evidence]:
    if not isinstance(manifest, list) or not manifest:
        raise LedgerError("result requires report and artifact evidence")
    checked: list[Evidence] = []
    for raw in manifest:
        item = obj(raw)
        actual = evidence(root, item.get("path"))
        if actual["sha256"] != text(item.get("sha256")):
            raise LedgerError(f"evidence changed: {actual['path']}")
        checked.append(actual)
    return checked


def atomic_json(path: Path, payload: object, *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encode(payload) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if replace:
            os.replace(temporary, path)
        elif os.name == "nt":
            # Windows rename is atomic and refuses an existing destination.
            os.rename(temporary, path)
        else:
            # A complete file becomes visible atomically; an existing event is never replaced.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, root TEXT NOT NULL,
 host TEXT NOT NULL, goal TEXT NOT NULL, plan TEXT NOT NULL, concurrency_limit INTEGER NOT NULL, UNIQUE(host, root));
CREATE TABLE tasks (project TEXT NOT NULL REFERENCES projects(id), workflow TEXT NOT NULL,
 id TEXT NOT NULL, spec TEXT NOT NULL, revision INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, run_state TEXT NOT NULL DEFAULT 'unknown', thread TEXT NOT NULL DEFAULT '',
 report TEXT NOT NULL DEFAULT '', manifest TEXT NOT NULL DEFAULT '[]', review TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '', observed_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(project, workflow, id));
CREATE TABLE events (id TEXT PRIMARY KEY, hash TEXT NOT NULL, project TEXT NOT NULL,
 payload TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE reviews (id TEXT PRIMARY KEY, project TEXT NOT NULL, workflow TEXT NOT NULL,
 task TEXT NOT NULL, revision INTEGER NOT NULL, attempt INTEGER NOT NULL, manifest TEXT NOT NULL,
 state TEXT NOT NULL, owner TEXT, epoch INTEGER, token TEXT, created_at TEXT NOT NULL,
 closed_at TEXT, evidence TEXT, note TEXT, UNIQUE(project,workflow,task,revision,attempt));
CREATE TABLE actions (id TEXT PRIMARY KEY, project TEXT NOT NULL, workflow TEXT NOT NULL,
 task TEXT NOT NULL, revision INTEGER NOT NULL, attempt INTEGER NOT NULL,
 state TEXT NOT NULL, receipt TEXT, created_at TEXT NOT NULL);
CREATE TABLE changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
 project TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE imports (path TEXT PRIMARY KEY, hash TEXT NOT NULL, result TEXT NOT NULL);
"""


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise LedgerError("ledger does not exist; run init explicitly")
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if self._meta(db, "schema") not in {"1", "2"}:
                raise LedgerError("unsupported ledger schema")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str) -> str:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise LedgerError(f"missing ledger setting: {key}")
        return str(row["value"])

    @staticmethod
    def _audit(db: sqlite3.Connection, kind: str, project: str | None, payload: object) -> None:
        db.execute("INSERT INTO changes(kind,project,payload,created_at) VALUES(?,?,?,?)",
                   (kind, project, encode(payload), now()))

    @staticmethod
    def _task(db: sqlite3.Connection, key: TaskKey) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE project=? AND workflow=? AND id=?",
                         (key["project"], key["workflow"], key["task"])).fetchone()
        if row is None:
            raise LedgerError("unknown project/workflow/task")
        return row

    @staticmethod
    def _project(db: sqlite3.Connection, project: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if row is None:
            raise LedgerError("unknown project")
        return row

    def _owner(self, db: sqlite3.Connection, owner: str, epoch: int) -> None:
        if self._meta(db, "supervisor") != owner or int(self._meta(db, "epoch")) != epoch:
            raise LedgerError("supervisor ownership changed; reload before writing")

    def initialize(self, supervisor: str, active_limit: int = 3, review_limit: int = 4) -> None:
        supervisor = text(supervisor)
        integer(active_limit)
        integer(review_limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        db = sqlite3.connect(self.path)
        try:
            db.executescript(SCHEMA + helm_context.CONTEXT_SCHEMA)
            db.executemany("INSERT INTO meta VALUES(?,?)", [("schema", "2"), ("supervisor", supervisor),
                ("epoch", "1"), ("active_limit", str(active_limit)), ("review_limit", str(review_limit)),
                ("last_review_project", "")])
            self._audit(db, "initialized", None, {"supervisor": supervisor})
            db.commit()
        finally:
            db.close()

    def register(self, config: dict[str, object], owner: str, epoch: int) -> None:
        project = identifier(config.get("id"))
        root = Path(text(config.get("root"))).resolve(strict=True)
        if not root.is_dir():
            raise LedgerError("project root must be a directory")
        plan = evidence(root, config.get("plan"))["path"]
        workflow = helm.parse_workflow(config.get("workflow"))
        identifier(workflow.workflow_id)
        raw_tasks = obj(config.get("workflow"))["tasks"]
        if not isinstance(raw_tasks, list):
            raise LedgerError("tasks must be a list")
        for task in workflow.tasks:
            identifier(task.task_id)
            if task.status != "planned" or task.run_state != "unknown":
                raise LedgerError("new managed workflows must start planned/unknown; no implicit acceptance import")
            for write in task.writes:
                scoped(root, write)
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            host = text(config.get("host"))
            registered_host = db.execute("SELECT host FROM projects LIMIT 1").fetchone()
            if registered_host and registered_host["host"] != host:
                raise LedgerError("this runtime manages one host; use a separate ledger for another host")
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?)", (project, text(config.get("title")),
                str(root), host, text(config.get("goal")), plan, workflow.concurrency_limit))
            for raw, task in zip(raw_tasks, workflow.tasks):
                spec = obj(raw)
                db.execute("INSERT INTO tasks(project,workflow,id,spec,revision,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project, workflow.workflow_id, task.task_id, encode(spec), task.version, "planned", now()))
            self._audit(db, "project.registered", project, config)
        scoped(root, ".session-helm/outbox").mkdir(parents=True, exist_ok=True)

    def limits(self, active: int, reviews: int, owner: str, epoch: int) -> None:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            db.execute("UPDATE meta SET value=? WHERE key='active_limit'", (str(integer(active)),))
            db.execute("UPDATE meta SET value=? WHERE key='review_limit'", (str(integer(reviews)),))
            self._audit(db, "limits.updated", None, {"active_limit": active, "review_limit": reviews})

    def _deps_valid(self, db: sqlite3.Connection, row: sqlite3.Row, *, files: bool = False,
                    visiting: frozenset[str] = frozenset()) -> bool:
        key = f"{row['project']}/{row['workflow']}/{row['id']}"
        if key in visiting:
            return False
        spec = obj(json.loads(row["spec"]))
        dependencies = spec.get("depends_on", [])
        if not isinstance(dependencies, list):
            return False
        for dep_value in dependencies:
            dep = obj(dep_value)
            parent = self._task(db, {"project": row["project"], "workflow": row["workflow"], "task": text(dep.get("id"))})
            if parent["status"] != "accepted" or parent["revision"] != dep.get("version"):
                return False
            if not self._deps_valid(db, parent, files=files, visiting=visiting | {key}):
                return False
            if files:
                verify_evidence(Path(self._project(db, row["project"])["root"]), json.loads(parent["manifest"]))
        return True

    def _ingest(self, db: sqlite3.Connection, project: str, event: dict[str, object]) -> str:
        event_id = identifier(event.get("event_id"))
        payload = encode(event)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        prior = db.execute("SELECT hash FROM events WHERE id=?", (event_id,)).fetchone()
        if prior:
            if prior["hash"] != payload_hash:
                raise LedgerError("event ID collision: different content")
            return "duplicate"
        if event.get("project") != project:
            raise LedgerError("event project does not match its registered outbox")
        key: TaskKey = {"project": project, "workflow": identifier(event.get("workflow")), "task": identifier(event.get("task"))}
        row = self._task(db, key)
        if event.get("thread") != row["thread"] or event.get("host") != self._project(db, project)["host"]:
            raise LedgerError("event source is not the assigned thread/host")
        disposition, reason = "applied", ""
        kind = text(event.get("kind"))
        if kind not in {"STARTED", "MILESTONE", "RESULT", "BLOCKED", "NEEDS_INPUT", "NEEDS_APPROVAL", "FAILED"}:
            raise LedgerError("unsupported event kind")
        if integer(event.get("revision")) != row["revision"] or integer(event.get("attempt")) != row["attempt"]:
            disposition, reason = "stale", "superseded task revision or attempt"
        elif row["status"] in {"accepted", "needs_revision", "paused", "detached"}:
            disposition, reason = "ignored", "task no longer accepts executor events"
        elif kind == "RESULT":
            root = Path(self._project(db, project)["root"])
            manifest = verify_evidence(root, event.get("evidence"))
            report = text(event.get("report"))
            if report not in {e["path"] for e in manifest}:
                raise LedgerError("report must be included in evidence")
            old = db.execute("SELECT manifest FROM reviews WHERE project=? AND workflow=? AND task=? AND revision=? AND attempt=?",
                (project, key["workflow"], key["task"], row["revision"], row["attempt"])).fetchone()
            if old:
                if old["manifest"] != encode(manifest):
                    raise LedgerError("a different result already exists; revise the task before resubmitting")
                disposition, reason = "duplicate_result", "same result already queued"
            else:
                db.execute("INSERT INTO reviews(id,project,workflow,task,revision,attempt,manifest,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, project, key["workflow"], key["task"], row["revision"], row["attempt"], encode(manifest), "queued", now()))
                db.execute("UPDATE tasks SET status='reported',report=?,manifest=?,note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                    (report, encode(manifest), text(event.get("summary")), now(), project, key["workflow"], key["task"]))
        elif row["status"] == "reported":
            disposition, reason = "ignored", "result is already awaiting review"
        else:
            status = {"STARTED": "running", "MILESTONE": "running", "BLOCKED": "waiting_input",
                "NEEDS_INPUT": "waiting_input", "NEEDS_APPROVAL": "waiting_approval", "FAILED": "failed"}[kind]
            db.execute("UPDATE tasks SET status=?,note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (status, text(event.get("summary")), now(), project, key["workflow"], key["task"]))
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (event_id, payload_hash, project, payload, disposition, reason, now()))
        self._audit(db, "event." + disposition, project, {"event_id": event_id, "kind": kind, "task": key["task"], "reason": reason})
        return disposition

    def ingest(self, project: str, event: dict[str, object]) -> str:
        with self.connection(write=True) as db:
            return self._ingest(db, project, event)

    def scan(self) -> dict[str, int]:
        counts = {"imported": 0, "rejected": 0, "unchanged": 0}
        with self.connection() as db:
            projects = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        for project in projects:
            root = Path(project["root"])
            try:
                outbox = scoped(root, ".session-helm/outbox")
                paths = sorted(outbox.glob("*.json"))
            except (OSError, LedgerError) as exc:
                self._reject_import(str(root), "unavailable:" + str(exc), project["id"], str(exc), counts)
                continue
            for path in paths:
                if not path.resolve().is_relative_to(outbox.resolve()):
                    continue
                try:
                    info = path.stat()
                    if info.st_size > 256 * 1024:
                        self._reject_import(str(path), f"oversized:{info.st_size}:{info.st_mtime_ns}", project["id"],
                            "event exceeds 256 KiB; put detailed content in the report", counts)
                        continue
                    file_hash = digest(path)
                except OSError as exc:
                    self._reject_import(str(path), "unreadable:" + str(exc), project["id"], str(exc), counts)
                    continue
                with self.connection(write=True) as db:
                    prior = db.execute("SELECT hash FROM imports WHERE path=?", (str(path),)).fetchone()
                    if prior and prior["hash"] == file_hash:
                        counts["unchanged"] += 1
                        continue
                    db.execute("SAVEPOINT event_import")
                    try:
                        if path.stat().st_size > 256 * 1024:
                            raise LedgerError("event exceeds 256 KiB; put detailed content in the report")
                        result = self._ingest(db, project["id"], read_json(path))
                        counts["imported"] += 1
                    except (LedgerError, ValueError, OSError) as exc:
                        db.execute("ROLLBACK TO event_import")
                        result = "rejected: " + str(exc)
                        counts["rejected"] += 1
                        self._audit(db, "event.rejected", project["id"], {"file": path.name, "reason": str(exc)})
                    finally:
                        db.execute("RELEASE event_import")
                    db.execute("INSERT INTO imports VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash,result=excluded.result",
                        (str(path), file_hash, result))
        return counts

    def _reject_import(self, path: str, fingerprint: str, project: str, reason: str,
                       counts: dict[str, int]) -> None:
        with self.connection(write=True) as db:
            prior = db.execute("SELECT hash FROM imports WHERE path=?", (path,)).fetchone()
            if prior and prior["hash"] == fingerprint:
                counts["unchanged"] += 1
                return
            db.execute("INSERT INTO imports VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash,result=excluded.result",
                (path, fingerprint, "rejected: " + reason))
            self._audit(db, "event.rejected", project, {"file": Path(path).name, "reason": reason})
            counts["rejected"] += 1

    @staticmethod
    def _busy(row: sqlite3.Row) -> bool:
        return row["status"] in {"running", "reported"} or (row["attempt"] > 0 and row["run_state"] != "idle")

    @staticmethod
    def _occupies_slot(row: sqlite3.Row) -> bool:
        return row["status"] == "running" or (row["attempt"] > 0 and row["run_state"] != "idle")

    def _write_conflict(self, db: sqlite3.Connection, left: sqlite3.Row, right: sqlite3.Row) -> bool:
        lp, rp = self._project(db, left["project"]), self._project(db, right["project"])
        if lp["host"] != rp["host"]:
            return False
        left_paths = obj(json.loads(left["spec"]))["write_paths"]
        right_paths = obj(json.loads(right["spec"]))["write_paths"]
        if not isinstance(left_paths, list) or not isinstance(right_paths, list):
            raise LedgerError("invalid write scopes")
        for a in left_paths:
            a_path = scoped(Path(lp["root"]), a)
            for b in right_paths:
                b_path = scoped(Path(rp["root"]), b)
                if a_path.is_relative_to(b_path) or b_path.is_relative_to(a_path):
                    return True
        return False

    def _ready_reason(self, db: sqlite3.Connection, row: sqlite3.Row) -> str:
        if row["status"] != "planned":
            return "task is not planned"
        if row["attempt"] and row["run_state"] != "idle":
            return "previous execution has not been verified idle"
        if self._meta(db, "schema") == "2" and db.execute("SELECT 1 FROM decisions WHERE project=? AND workflow=? AND task=? AND state='open'", (row["project"], row["workflow"], row["id"])).fetchone():
            return "user decision is still open"
        if not self._deps_valid(db, row):
            return "dependency is not accepted at the required revision"
        pending = db.execute("SELECT count(*) FROM reviews WHERE state IN ('queued','claimed')").fetchone()[0]
        if pending >= int(self._meta(db, "review_limit")):
            return "review backlog limit reached"
        all_tasks = db.execute("SELECT * FROM tasks").fetchall()
        busy = [r for r in all_tasks if self._busy(r)]
        executing = [r for r in all_tasks if self._occupies_slot(r)]
        if len(executing) >= int(self._meta(db, "active_limit")):
            return "global execution limit reached"
        if sum(r["project"] == row["project"] for r in executing) >= self._project(db, row["project"])["concurrency_limit"]:
            return "project execution limit reached"
        if any(self._write_conflict(db, row, other) for other in busy):
            return "declared write scope conflicts with unsettled execution"
        return ""

    def prepare(self, key: TaskKey, owner: str, epoch: int) -> dict[str, object]:
        """Reserve one dispatch; the caller performs and reconciles the native tool call."""
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            helm_context.require_focus(self, db, key["project"], owner, epoch)
            row = self._task(db, key)
            reason = self._ready_reason(db, row)
            if reason:
                raise LedgerError(reason)
            self._deps_valid(db, row, files=True)
            action = uuid.uuid4().hex
            attempt = row["attempt"] + 1
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)", (action, key["project"], key["workflow"],
                key["task"], row["revision"], attempt, "prepared", None, now()))
            db.execute("UPDATE tasks SET attempt=?,status='running',run_state='unknown',observed_at=NULL,note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (attempt, "已预留派发；等待原生工具回执", now(), key["project"], key["workflow"], key["task"]))
            result = {**key, "action_id": action, "revision": row["revision"], "attempt": attempt}
            self._audit(db, "dispatch.prepared", key["project"], result)
            return result

    def bind(self, action_id: str, thread: str, receipt: str, owner: str, epoch: int) -> dict[str, object]:
        thread, receipt = text(thread), text(receipt)
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if action is None:
                raise LedgerError("unknown dispatch intent")
            if action["state"] not in {"prepared", "delivered"}:
                raise LedgerError("dispatch intent was cancelled")
            key: TaskKey = {"project": action["project"], "workflow": action["workflow"], "task": action["task"]}
            row = self._task(db, key)
            if row["revision"] != action["revision"] or row["attempt"] != action["attempt"]:
                raise LedgerError("dispatch has been superseded")
            if action["state"] == "delivered" and (row["thread"] != thread or action["receipt"] != receipt):
                raise LedgerError("delivery already recorded with different evidence")
            binding = {**key, "revision": row["revision"], "attempt": row["attempt"],
                       "thread": thread, "host": self._project(db, key["project"])["host"]}
            if action["state"] == "delivered":
                return binding
            db.execute("UPDATE actions SET state='delivered',receipt=? WHERE id=?", (receipt, action_id))
            db.execute("UPDATE tasks SET thread=?,note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (thread, "原生工具已接受派发；运行情况待核实", now(), key["project"], key["workflow"], key["task"]))
            self._audit(db, "dispatch.delivered", key["project"], {**binding, "action_id": action_id, "receipt": receipt})
            return binding

    def cancel_dispatch(self, action_id: str, evidence_ref: str, owner: str, epoch: int) -> None:
        """Reconcile a proven unsent intent. This never stops a chat or process."""
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if action is None or action["state"] != "prepared":
                raise LedgerError("only an undelivered dispatch intent can be cancelled")
            key: TaskKey = {"project": action["project"], "workflow": action["workflow"], "task": action["task"]}
            row = self._task(db, key)
            if (row["revision"], row["attempt"]) != (action["revision"], action["attempt"]):
                raise LedgerError("dispatch has been superseded")
            evidence_ref = text(evidence_ref)
            db.execute("UPDATE actions SET state='cancelled',receipt=? WHERE id=?", (evidence_ref, action_id))
            db.execute("UPDATE tasks SET status='planned',run_state='idle',thread='',note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                ("已核实未发送；撤销本次派发预留", now(), key["project"], key["workflow"], key["task"]))
            self._audit(db, "dispatch.cancelled", key["project"], {"action_id": action_id, "evidence": evidence_ref})

    def observe(self, key: TaskKey, thread: str, run_state: str, evidence_ref: str, owner: str, epoch: int) -> None:
        if run_state not in helm.RUN_STATES:
            raise LedgerError("unsupported runtime observation")
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            row = self._task(db, key)
            if row["thread"] != thread:
                raise LedgerError("observation belongs to a different thread")
            db.execute("UPDATE tasks SET run_state=?,observed_at=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (run_state, now(), now(), key["project"], key["workflow"], key["task"]))
            self._audit(db, "runtime.observed", key["project"], {**key, "run_state": run_state, "evidence": text(evidence_ref)})

    def claim(self, owner: str, epoch: int, review_id: str | None = None) -> dict[str, object]:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            active = db.execute("SELECT * FROM reviews WHERE state='claimed'").fetchone()
            if active:
                if review_id and review_id != active["id"]:
                    raise LedgerError("finish or release the current review first")
                return dict(active)
            rows = db.execute("SELECT * FROM reviews WHERE state='queued'").fetchall()
            if review_id:
                rows = [row for row in rows if row["id"] == review_id]
            if not rows:
                raise LedgerError("no queued review")
            ranked = helm_context.queue(self, db)
            row = next(r for r in ranked if r["id"] in {item["id"] for item in rows})
            token = uuid.uuid4().hex
            db.execute("UPDATE reviews SET state='claimed',owner=?,epoch=?,token=? WHERE id=?", (owner, epoch, token, row["id"]))
            self._audit(db, "review.claimed", row["project"], {"review_id": row["id"], "owner": owner})
            return {**dict(row), "state": "claimed", "owner": owner, "epoch": epoch, "token": token}

    def release(self, review_id: str, token: str, owner: str, epoch: int) -> None:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            count = db.execute("UPDATE reviews SET state='queued',owner=NULL,epoch=NULL,token=NULL WHERE id=? AND state='claimed' AND token=? AND owner=? AND epoch=?",
                (review_id, token, owner, epoch)).rowcount
            if count != 1:
                raise LedgerError("review claim is no longer current")
            self._audit(db, "review.released", None, {"review_id": review_id})

    def decide(self, review_id: str, token: str, verdict: str, review_path: str, note: str,
               owner: str, epoch: int) -> None:
        if verdict not in {"accepted", "needs_revision", "waiting_input"}:
            raise LedgerError("unsupported review verdict")
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            review = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if review is None or review["state"] != "claimed" or review["token"] != token or review["epoch"] != epoch or review["owner"] != owner:
                raise LedgerError("review claim is no longer current")
            key: TaskKey = {"project": review["project"], "workflow": review["workflow"], "task": review["task"]}
            row = self._task(db, key)
            if (row["revision"], row["attempt"], row["status"]) != (review["revision"], review["attempt"], "reported"):
                raise LedgerError("task changed during review")
            if row["thread"] == owner:
                raise LedgerError("executor cannot independently accept its own result")
            helm_context.require_focus(self, db, key["project"], owner, epoch)
            root = Path(self._project(db, row["project"])["root"])
            review_evidence = evidence(root, review_path)
            if review_evidence["path"] in {e["path"] for e in json.loads(review["manifest"])}:
                raise LedgerError("review evidence must be separate from executor evidence")
            if verdict == "accepted":
                if self._meta(db, "schema") == "2" and db.execute("SELECT 1 FROM decisions WHERE project=? AND workflow=? AND task=? AND state='open'", (row["project"], row["workflow"], row["id"])).fetchone():
                    raise LedgerError("user decision is still open")
                if row["run_state"] != "idle":
                    raise LedgerError("verify executor and owned work have settled before accepting")
                verify_evidence(root, json.loads(review["manifest"]))
                if not self._deps_valid(db, row, files=True):
                    raise LedgerError("dependency changed during review")
            db.execute("UPDATE reviews SET state=?,closed_at=?,evidence=?,note=? WHERE id=?",
                (verdict, now(), encode(review_evidence), text(note), review_id))
            db.execute("UPDATE tasks SET status=?,review=?,note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (verdict, review_evidence["path"], text(note), now(), key["project"], key["workflow"], key["task"]))
            db.execute("UPDATE meta SET value=? WHERE key='last_review_project'", (row["project"],))
            self._audit(db, "review." + verdict, row["project"], {**key, "review_id": review_id, "evidence": review_evidence, "note": note})

    def revise(self, key: TaskKey, spec: dict[str, object], reason: str, owner: str, epoch: int) -> None:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            helm_context.require_focus(self, db, key["project"], owner, epoch)
            old = self._task(db, key)
            if spec.get("id") != key["task"] or integer(spec.get("version")) != old["revision"] + 1:
                raise LedgerError("revision must retain task ID and increase version by exactly one")
            spec = {**spec, "status": "planned", "run_state": "unknown"}
            siblings = [obj(json.loads(r["spec"])) for r in db.execute("SELECT * FROM tasks WHERE project=? AND workflow=?",
                (key["project"], key["workflow"])).fetchall() if r["id"] != key["task"]]
            helm.parse_workflow({"schema_version": 1, "workflow_id": key["workflow"], "updated_at": now(),
                "case_sensitive_paths": os.name != "nt", "concurrency_limit": 3, "tasks": siblings + [spec]})
            root = Path(self._project(db, key["project"])["root"])
            writes = spec.get("write_paths")
            if not isinstance(writes, list):
                raise LedgerError("write_paths must be a list")
            for path in writes:
                scoped(root, path)
            db.execute("UPDATE tasks SET spec=?,revision=?,status='planned',report='',manifest='[]',review='',note=?,updated_at=? WHERE project=? AND workflow=? AND id=?",
                (encode(spec), spec["version"], text(reason), now(), key["project"], key["workflow"], key["task"]))
            db.execute("UPDATE reviews SET state='superseded',closed_at=? WHERE project=? AND workflow=? AND task=? AND state IN ('queued','claimed')",
                (now(), key["project"], key["workflow"], key["task"]))
            if self._meta(db, "schema") == "2":
                db.execute("UPDATE decisions SET state='superseded' WHERE project=? AND workflow=? AND task=? AND state='open'", (key["project"], key["workflow"], key["task"]))
            self._audit(db, "task.revised", key["project"], {**key, "previous": json.loads(old["spec"]), "spec": spec, "reason": reason})

    def handoff(self, successor: str, owner: str, epoch: int, reason: str) -> int:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            successor = text(successor)
            db.execute("UPDATE meta SET value=? WHERE key='supervisor'", (successor,))
            db.execute("UPDATE meta SET value=? WHERE key='epoch'", (str(epoch + 1),))
            db.execute("UPDATE reviews SET state='queued',owner=NULL,epoch=NULL,token=NULL WHERE state='claimed'")
            if self._meta(db, "schema") == "2":
                db.execute("DELETE FROM focus")
            self._audit(db, "supervisor.handoff", None, {"from": owner, "to": successor, "epoch": epoch + 1, "reason": text(reason)})
            return epoch + 1

    def context(self, key: TaskKey) -> dict[str, object]:
        with self.connection() as db:
            row = self._task(db, key)
            project = self._project(db, key["project"])
            spec = obj(json.loads(row["spec"]))
            dependencies = spec["depends_on"]
            if not isinstance(dependencies, list):
                raise LedgerError("invalid dependency list")
            prior = []
            for value in dependencies:
                dependency = obj(value)
                parent = self._task(db, {**key, "task": text(dependency["id"])})
                prior.append({"task": parent["id"], "revision": parent["revision"], "status": parent["status"],
                    "report": parent["report"], "review": parent["review"]})
            return {"project": dict(project), "task": spec, "revision": row["revision"], "attempt": row["attempt"],
                "thread": row["thread"], "status": row["status"], "note": row["note"], "dependencies": prior,
                "supervisor": self._meta(db, "supervisor"), "epoch": int(self._meta(db, "epoch"))}

    def upgrade(self, backup: Path, owner: str, epoch: int) -> None:
        """Explicit v1 migration with a SQLite-consistent backup and a concurrent-write check."""
        backup = backup.resolve()
        if backup == self.path or backup.exists():
            raise LedgerError("migration backup must be a new, separate file")
        with self.connection() as db:
            self._owner(db, owner, epoch)
            if self._meta(db, "schema") != "1":
                raise LedgerError("only schema 1 needs upgrade")
            sequence = db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("xb"):
                pass
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
            finally:
                target.close()
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            if self._meta(db, "schema") != "1" or sequence != db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]:
                raise LedgerError("ledger changed during backup; stop writers and retry with a new backup path")
            for statement in helm_context.CONTEXT_SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("UPDATE meta SET value='2' WHERE key='schema'")
            self._audit(db, "schema.upgraded", None, {"backup": str(backup)})

    def save_context(self, project: str, capsule: dict[str, object], expected: int, owner: str, epoch: int) -> int:
        """A bounded summary with project-local evidence; compare-and-swap prevents lost edits."""
        from helm_context import bounded_text
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            p = self._project(db, project)
            if self._meta(db, "schema") != "2":
                raise LedgerError("upgrade the ledger to enable supervisor context")
            prior = db.execute("SELECT version FROM contexts WHERE project=?", (project,)).fetchone()
            if integer(expected, 0) != (prior["version"] if prior else 0):
                raise LedgerError("context version changed; reload before updating")
            if capsule.get("project") != project:
                raise LedgerError("capsule project does not match target")
            brief: dict[str, object] = {"goal": bounded_text(capsule.get("goal")), "next_step": bounded_text(capsule.get("next_step"))}
            for field in ("rules", "definitions", "decisions", "open_questions"):
                values = capsule.get(field, [])
                if not isinstance(values, list) or len(values) > 12:
                    raise LedgerError(f"{field} must contain at most 12 short items")
                brief[field] = [bounded_text(v, 800) for v in values]
            paths = capsule.get("sources")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 12:
                raise LedgerError("capsule requires 1-12 project-local source files")
            root = Path(p["root"])
            sources = [evidence(root, path) for path in dict.fromkeys([p["plan"], *paths])]
            if len(encode(brief)) > 10000:
                raise LedgerError("capsule exceeds 10000 characters; link detailed evidence")
            db.execute("INSERT INTO contexts VALUES(?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET version=excluded.version,brief=excluded.brief,sources=excluded.sources,updated_at=excluded.updated_at",
                (project, expected + 1, encode(brief), encode(sources), now()))
            self._audit(db, "context.saved", project, {"version": expected + 1, "sources": sources})
            return expected + 1

    def focus(self, project: str, owner: str, epoch: int) -> dict[str, object]:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            if self._meta(db, "schema") != "2":
                raise LedgerError("upgrade the ledger to enable supervisor context")
            active = db.execute("SELECT * FROM focus WHERE slot=1").fetchone()
            if active and active["project"] != project:
                raise LedgerError("checkpoint the current project before switching focus")
            claimed = db.execute("SELECT project FROM reviews WHERE state='claimed'").fetchone()
            if claimed and claimed["project"] != project:
                raise LedgerError("release the current review before switching focus")
            packet = helm_context.packet(self, db, project)
            db.execute("INSERT INTO focus VALUES(1,?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET project=excluded.project,fingerprint=excluded.fingerprint,owner=excluded.owner,epoch=excluded.epoch,loaded_at=excluded.loaded_at",
                (project, packet["fingerprint"], owner, epoch, now()))
            self._audit(db, "context.loaded", project, {"version": packet["context_version"], "fingerprint": packet["fingerprint"]})
            return packet

    def checkpoint(self, project: str, summary: str, next_step: str, questions: list[str], owner: str, epoch: int) -> None:
        from helm_context import bounded_text
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            active = db.execute("SELECT * FROM focus WHERE slot=1").fetchone()
            if not active or active["project"] != project:
                raise LedgerError("checkpoint must belong to the focused project")
            if db.execute("SELECT 1 FROM reviews WHERE state='claimed'").fetchone():
                raise LedgerError("decide or release the active review before checkpoint")
            loaded = db.execute("SELECT payload FROM changes WHERE kind='context.loaded' AND project=? ORDER BY seq DESC LIMIT 1", (project,)).fetchone()
            loaded_context = obj(json.loads(loaded["payload"]))
            if loaded_context["fingerprint"] != active["fingerprint"]:
                raise LedgerError("loaded context audit does not match current focus")
            if len(questions) > 12:
                raise LedgerError("keep at most 12 open questions in the checkpoint")
            db.execute("INSERT INTO checkpoints(project,context_version,summary,next_step,questions,owner,epoch,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project, loaded_context["version"], bounded_text(summary), bounded_text(next_step), encode([bounded_text(q, 800) for q in questions]), owner, epoch, now()))
            db.execute("DELETE FROM focus")
            self._audit(db, "context.checkpointed", project, {"summary": summary, "next_step": next_step})

    def portfolio(self) -> dict[str, object]:
        """The supervisor's global view contains routing and decisions, not report bodies."""
        snapshot = self.snapshot()
        projects = []
        with self.connection() as db:
            for p in snapshot["projects"]:
                tasks = [t for t in snapshot["tasks"] if t["project"] == p["id"]]
                checkpoint = db.execute("SELECT summary,next_step,context_version FROM checkpoints WHERE project=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone() if self._meta(db, "schema") == "2" else None
                context = db.execute("SELECT version FROM contexts WHERE project=?", (p["id"],)).fetchone() if self._meta(db, "schema") == "2" else None
                projects.append({"project": p["id"], "title": p["title"], "goal": p["goal"][:240],
                    "accepted": sum(t["effective_status"] == "accepted" for t in tasks), "total": len(tasks),
                    "review_backlog": sum(r["project"] == p["id"] for r in snapshot["reviews"]),
                    "context_version": context["version"] if context else None,
                    "checkpoint": dict(checkpoint) if checkpoint else None})
        return {"supervisor": snapshot["supervisor"], "epoch": snapshot["epoch"], "sequence": snapshot["sequence"],
            "projects": projects, "reviews": snapshot["reviews"], "decisions": snapshot["decisions"],
            "pending_actions": snapshot["pending_actions"], "focus": snapshot["focus"]}

    def request_decision(self, key: TaskKey, question: str, impact: str, options: list[str], owner: str, epoch: int) -> str:
        from helm_context import bounded_text
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            row = self._task(db, key)
            if not 2 <= len(options) <= 5:
                raise LedgerError("decision needs 2-5 concrete options")
            decision_id = uuid.uuid4().hex
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (decision_id, key["project"], key["workflow"], key["task"], row["revision"],
                bounded_text(question), bounded_text(impact), encode([bounded_text(o, 500) for o in options]), "open", None, None, now()))
            self._audit(db, "decision.requested", key["project"], {"id": decision_id, "question": question})
            return decision_id

    def resolve_decision(self, decision_id: str, answer: str, source: str, owner: str, epoch: int) -> None:
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch)
            row = db.execute("SELECT * FROM decisions WHERE id=? AND state='open'", (decision_id,)).fetchone()
            if row is None:
                raise LedgerError("no open decision with this ID")
            proof = evidence(Path(self._project(db, row["project"])["root"]), source)
            db.execute("UPDATE decisions SET state='resolved',answer=?,evidence=? WHERE id=?", (text(answer), encode(proof), decision_id))
            self._audit(db, "decision.resolved", row["project"], {"id": decision_id, "answer": answer, "evidence": proof})

    def artifact(self, project: str, workflow: str, task: str, kind: str) -> tuple[Path, str]:
        """Only current registered report/review evidence is exposed; no arbitrary paths."""
        with self.connection() as db:
            row = self._task(db, {"project": project, "workflow": workflow, "task": task})
            root = Path(self._project(db, project)["root"])
            if kind == "report":
                manifest = json.loads(row["manifest"])
                candidates = [m for m in manifest if m["path"] == row["report"]]
            elif kind == "review":
                record = db.execute("SELECT evidence FROM reviews WHERE project=? AND workflow=? AND task=? AND revision=? AND attempt=? AND evidence IS NOT NULL", (project, workflow, task, row["revision"], row["attempt"])).fetchone()
                candidates = [json.loads(record["evidence"])] if record else []
            else:
                raise LedgerError("only current report or review evidence is available")
            checked = verify_evidence(root, candidates)
            path = scoped(root, checked[0]["path"], exists=True)
            if path.stat().st_size > 1024 * 1024:
                raise LedgerError("evidence exceeds 1 MiB; open the registered file locally")
            return path, checked[0]["sha256"]

    def snapshot(self) -> dict[str, object]:
        with self.connection() as db:
            task_views = []
            for row in db.execute("SELECT * FROM tasks ORDER BY project,workflow,id").fetchall():
                spec = obj(json.loads(row["spec"]))
                stale = not self._deps_valid(db, row)
                task_views.append({"project": row["project"], "workflow": row["workflow"], "id": row["id"],
                    "title": spec["title"], "revision": row["revision"], "attempt": row["attempt"], "status": row["status"],
                    "effective_status": "needs_recheck" if stale and row["status"] == "accepted" else row["status"],
                    "dependencies": spec["depends_on"], "run_state": row["run_state"], "thread": row["thread"],
                    "report": row["report"], "review": row["review"], "note": row["note"],
                    "observed_at": row["observed_at"], "updated_at": row["updated_at"], "ready_reason": self._ready_reason(db, row)})
            modern = self._meta(db, "schema") == "2"
            focus = db.execute("SELECT project,loaded_at FROM focus WHERE slot=1").fetchone() if modern else None
            return {"schema_version": int(self._meta(db, "schema")), "sequence": db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0],
                "generated_at": now(), "supervisor": self._meta(db, "supervisor"), "epoch": int(self._meta(db, "epoch")),
                "active_limit": int(self._meta(db, "active_limit")), "review_limit": int(self._meta(db, "review_limit")),
                "projects": [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY id").fetchall()],
                "tasks": task_views, "reviews": [{k: v for k, v in row.items() if k not in {"token", "manifest"}} for row in helm_context.queue(self, db)],
                "focus": dict(focus) if focus else None,
                "decisions": [dict(r) for r in db.execute("SELECT * FROM decisions WHERE state='open' ORDER BY created_at")] if modern else [],
                "pending_actions": [dict(row) for row in db.execute("SELECT * FROM actions WHERE state='prepared'").fetchall()],
                "recent_changes": [dict(row) for row in db.execute("SELECT * FROM changes ORDER BY seq DESC LIMIT 20").fetchall()]}

    def changes(self, after: int, limit: int = 100) -> list[dict[str, object]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()]

    def sequence(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0])

    def export(self, project_id: str, workflow: str) -> dict[str, object]:
        """Produce a read-only v1 snapshot for the existing dependency checker."""
        with self.connection() as db:
            project = self._project(db, project_id)
            rows = db.execute("SELECT * FROM tasks WHERE project=? AND workflow=? ORDER BY id", (project_id, workflow)).fetchall()
            if not rows:
                raise LedgerError("unknown workflow")
            tasks = [{**obj(json.loads(row["spec"])), "status": row["status"], "run_state": row["run_state"],
                "workspace": f"{project['host']}:{Path(project['root']).as_posix()}", "report": row["report"],
                "review": row["review"], "reason": row["note"]} for row in rows]
            return {"schema_version": 1, "workflow_id": workflow, "updated_at": max(row["updated_at"] for row in rows),
                "concurrency_limit": project["concurrency_limit"], "case_sensitive_paths": os.name != "nt", "tasks": tasks}


def publish(root: Path, binding: dict[str, object], kind: str, summary: str,
            report: str | None = None, artifacts: list[str] | None = None,
            event_id: str | None = None) -> Path:
    """Create one immutable outbox event; native messaging is the executor's next step."""
    root = root.resolve(strict=True)
    payload = {name: binding[name] for name in ("project", "workflow", "task", "revision", "attempt", "thread", "host")}
    payload.update({"event_id": identifier(event_id or uuid.uuid4().hex), "kind": kind, "summary": text(summary), "occurred_at": now()})
    if kind == "RESULT":
        if report is None:
            raise LedgerError("RESULT requires a report")
        manifest = [evidence(root, path) for path in dict.fromkeys([report, *(artifacts or [])])]
        payload.update({"report": manifest[0]["path"], "evidence": manifest})
    destination = scoped(root, f".session-helm/outbox/{payload['event_id']}.json")
    if destination.exists():
        raise LedgerError("event already exists; resend the original event instead of rewriting it")
    atomic_json(destination, payload, replace=False)
    return destination
