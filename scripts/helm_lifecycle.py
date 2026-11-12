"""Versioned workflow growth, non-destructive archives and delivery evidence."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helm_store import Ledger, TaskKey

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_state (
 project TEXT NOT NULL, workflow TEXT NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 PRIMARY KEY(project,workflow));
CREATE TABLE IF NOT EXISTS context_views (
 project TEXT PRIMARY KEY, owner TEXT NOT NULL, epoch INTEGER NOT NULL,
 scope TEXT NOT NULL, packet TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_gates (
 project TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
 evidence TEXT NOT NULL, tasks TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(project,id));
CREATE TABLE IF NOT EXISTS prechecks (
 review_id TEXT NOT NULL, reviewer TEXT NOT NULL, evidence TEXT NOT NULL,
 note TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(review_id,reviewer));
CREATE INDEX IF NOT EXISTS reviews_queue ON reviews(state,created_at);
CREATE INDEX IF NOT EXISTS changes_project_kind ON changes(project,kind,seq);
"""


def install(db: sqlite3.Connection) -> None:
    from helm_store import now
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR IGNORE INTO workflow_state(project,workflow,generation,state,updated_at) SELECT DISTINCT project,workflow,1,'active',? FROM tasks", (now(),))


def active(ledger: Ledger, db: sqlite3.Connection, project: str, workflow: str) -> bool:
    if ledger._meta(db, 'schema') != '3':
        return True
    row = db.execute('SELECT state FROM workflow_state WHERE project=? AND workflow=?', (project, workflow)).fetchone()
    return row is not None and row['state'] == 'active'


def require_v3(ledger: Ledger, db: sqlite3.Connection) -> None:
    from helm_store import LedgerError
    if ledger._meta(db, 'schema') != '3':
        raise LedgerError('upgrade with a backup to enable schema 3 lifecycle features')


def checked_prechecks(ledger: Ledger, db: sqlite3.Connection, review_id: str) -> list[dict[str, object]]:
    from helm_store import LedgerError, verify_evidence
    if ledger._meta(db, 'schema') != '3':
        return []
    review = db.execute('SELECT * FROM reviews WHERE id=?', (review_id,)).fetchone()
    if not review or review['state'] not in {'queued', 'claimed'}:
        return []
    root = Path(ledger._project(db, review['project'])['root'])
    result = []
    for row in db.execute('SELECT * FROM prechecks WHERE review_id=? ORDER BY created_at', (review_id,)):
        item = dict(row)
        try:
            verify_evidence(root, json.loads(review['manifest']))
            verify_evidence(root, [json.loads(row['evidence'])])
            item['valid'] = True
        except (LedgerError, OSError) as exc:
            item['valid'] = False
            item['error'] = str(exc)
        result.append(item)
    return result


def delivery(ledger: Ledger, db: sqlite3.Connection, project: str) -> dict[str, object]:
    from helm_store import LedgerError, verify_evidence
    if ledger._meta(db, 'schema') != '3':
        return {'project': project, 'state': 'undefined', 'gates': []}
    root = Path(ledger._project(db, project)['root'])
    gates = []
    for row in db.execute('SELECT * FROM delivery_gates WHERE project=? ORDER BY id', (project,)):
        gate = {k: row[k] for k in ('id', 'title', 'state', 'reason')}
        if row['state'] == 'passed':
            try:
                verify_evidence(root, [json.loads(row['evidence'])])
                for ref in json.loads(row['tasks']):
                    task = ledger._task(db, {'project': project, 'workflow': ref['workflow'], 'task': ref['task']})
                    if (task['revision'], task['attempt'], task['manifest'], task['status']) != (ref['revision'], ref['attempt'], ref['manifest'], 'accepted') or not ledger._deps_valid(db, task, files=True):
                        raise LedgerError('bound task or its dependency changed')
                    verify_evidence(root, json.loads(task['manifest']))
            except (LedgerError, OSError) as exc:
                gate['state'], gate['reason'] = 'needs_recheck', str(exc)
        gates.append(gate)
    unfinished = any(r['status'] != 'accepted' or not ledger._deps_valid(db, r) for r in db.execute('SELECT * FROM tasks WHERE project=?', (project,)))
    state = 'undefined' if not gates else ('passed' if all(g['state'] == 'passed' for g in gates) and not unfinished else 'pending')
    return {'project': project, 'state': state, 'gates': gates}


class LifecycleMixin:
    def append_tasks(self: Ledger, project: str, workflow: str, tasks: list[dict[str, object]], expected: int,
                     reason: str, owner: str, epoch: int) -> dict[str, object]:
        from helm_store import LedgerError, encode, identifier, integer, now, obj, scoped, text
        import helm
        import helm_context
        project, workflow = identifier(project), identifier(workflow)
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch); require_v3(self, db)
            helm_context.require_focus(self, db, project, owner, epoch)
            state = db.execute('SELECT * FROM workflow_state WHERE project=? AND workflow=?', (project, workflow)).fetchone()
            if integer(expected, 0) != (state['generation'] if state else 0):
                raise LedgerError('workflow generation changed; reload before appending')
            if state and state['state'] != 'active':
                raise LedgerError('restore the workflow before appending')
            if not tasks or len(tasks) > 100:
                raise LedgerError('append 1-100 tasks per transaction')
            siblings = [obj(json.loads(r['spec'])) for r in db.execute('SELECT spec FROM tasks WHERE project=? AND workflow=?', (project, workflow))]
            root = Path(self._project(db, project)['root'])
            for spec in tasks:
                if spec.get('status') != 'planned' or spec.get('run_state', 'unknown') != 'unknown' or spec.get('version') != 1:
                    raise LedgerError('appended tasks must start planned/unknown at version 1')
            parsed = helm.parse_workflow({'schema_version': 1, 'workflow_id': workflow, 'updated_at': now(),
                'concurrency_limit': 3, 'case_sensitive_paths': os.name != 'nt', 'tasks': siblings + tasks})
            for task in parsed.tasks:
                identifier(task.task_id)
                for path in task.writes:
                    scoped(root, path)
            for spec in tasks:
                db.execute("INSERT INTO tasks(project,workflow,id,spec,revision,status,updated_at) VALUES(?,?,?,?,1,'planned',?)", (project, workflow, spec['id'], encode(spec), now()))
            generation = expected + 1
            db.execute("INSERT INTO workflow_state VALUES(?,?,?,'active','',?) ON CONFLICT(project,workflow) DO UPDATE SET generation=excluded.generation,updated_at=excluded.updated_at", (project, workflow, generation, now()))
            result = {'project': project, 'workflow': workflow, 'generation': generation, 'added': [t['id'] for t in tasks]}
            self._audit(db, 'workflow.extended', project, {**result, 'reason': text(reason), 'tasks': tasks})
            return result

    def archive_workflow(self: Ledger, project: str, workflow: str, expected: int, summary: str,
                         owner: str, epoch: int, *, restore: bool = False) -> dict[str, object]:
        from helm_store import LedgerError, now, verify_evidence
        from helm_context import bounded_text, require_focus
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch); require_v3(self, db)
            require_focus(self, db, project, owner, epoch)
            state = db.execute('SELECT * FROM workflow_state WHERE project=? AND workflow=?', (project, workflow)).fetchone()
            if not state or state['generation'] != expected:
                raise LedgerError('workflow generation changed or workflow missing')
            if state['state'] != ('archived' if restore else 'active'):
                raise LedgerError('workflow already has the requested lifecycle state')
            root = Path(self._project(db, project)['root'])
            rows = db.execute('SELECT * FROM tasks WHERE project=? AND workflow=?', (project, workflow)).fetchall()
            for row in rows:
                if row['run_state'] != 'idle':
                    raise LedgerError('workflow must be settled before archive or restore')
                if not restore:
                    if row['status'] != 'accepted' or not self._deps_valid(db, row, files=True):
                        raise LedgerError('only effectively accepted workflows can be archived')
                    verify_evidence(root, json.loads(row['manifest']))
            for table, condition in [('reviews', "state IN ('queued','claimed')"), ('actions', "state='prepared'"), ('decisions', "state='open'")]:
                if db.execute(f'SELECT 1 FROM {table} WHERE project=? AND workflow=? AND {condition}', (project, workflow)).fetchone():
                    raise LedgerError('workflow has unresolved work')
            target = 'active' if restore else 'archived'
            db.execute('UPDATE workflow_state SET state=?,generation=generation+1,summary=?,updated_at=? WHERE project=? AND workflow=?', (target, bounded_text(summary), now(), project, workflow))
            self._audit(db, 'workflow.' + ('restored' if restore else 'archived'), project, {'workflow': workflow, 'summary': summary})
            return {'project': project, 'workflow': workflow, 'state': target, 'generation': expected + 1, 'retained_tasks': len(rows)}

    def list_tasks(self: Ledger, project: str, workflow: str | None = None, offset: int = 0, limit: int = 20,
                   *, include_archived: bool = False) -> dict[str, object]:
        from helm_store import integer
        integer(offset, 0); integer(limit)
        limit = min(limit, 50)
        with self.connection() as db:
            self._project(db, project)
            rows = [r for r in db.execute('SELECT * FROM tasks WHERE project=? ORDER BY workflow,id', (project,)) if (workflow is None or r['workflow'] == workflow) and (include_archived or active(self, db, project, r['workflow']))]
            items = [{k: r[k] for k in ('workflow', 'id', 'revision', 'attempt', 'status', 'report', 'review')} for r in rows[offset:offset + limit]]
            return {'project': project, 'tasks': items, 'total': len(rows), 'next_offset': offset + limit if offset + limit < len(rows) else None}

    def set_gate(self: Ledger, project: str, gate_id: str, title: str, state: str, reason: str,
                 source: str | None, task_keys: list[TaskKey], owner: str, epoch: int) -> None:
        from helm_store import LedgerError, encode, evidence, identifier, now
        from helm_context import bounded_text, require_focus
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch); require_v3(self, db)
            require_focus(self, db, project, owner, epoch)
            if state not in {'pending', 'passed', 'failed'}:
                raise LedgerError('gate state must be pending, passed or failed')
            if state == 'passed' and (not source or not task_keys):
                raise LedgerError('passed delivery needs independent evidence and accepted task bindings')
            root = Path(self._project(db, project)['root'])
            proof = evidence(root, source) if source else {}
            refs = []
            for key in task_keys:
                if key['project'] != project:
                    raise LedgerError('delivery task belongs to another project')
                row = self._task(db, key)
                if state == 'passed' and (row['status'] != 'accepted' or not self._deps_valid(db, row, files=True)):
                    raise LedgerError('delivery task is not effectively accepted')
                if state == 'passed':
                    from helm_store import verify_evidence
                    manifest = verify_evidence(root, json.loads(row['manifest']))
                    if proof['path'] in {m['path'] for m in manifest}:
                        raise LedgerError('delivery evidence must be separate from executor evidence')
                refs.append({'workflow': key['workflow'], 'task': key['task'], **{k: row[k] for k in ('revision', 'attempt', 'manifest')}})
            db.execute('INSERT INTO delivery_gates VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,id) DO UPDATE SET title=excluded.title,state=excluded.state,evidence=excluded.evidence,tasks=excluded.tasks,reason=excluded.reason,updated_at=excluded.updated_at',
                (project, identifier(gate_id), bounded_text(title, 160), state, encode(proof), encode(refs), bounded_text(reason), now()))
            self._audit(db, 'delivery.updated', project, {'gate': gate_id, 'state': state, 'reason': reason, 'evidence': proof, 'tasks': refs})

    def record_precheck(self: Ledger, review_id: str, reviewer: str, source: str, note: str,
                        owner: str, epoch: int) -> None:
        from helm_store import LedgerError, encode, evidence, now, text, verify_evidence
        with self.connection(write=True) as db:
            self._owner(db, owner, epoch); require_v3(self, db)
            review = db.execute('SELECT * FROM reviews WHERE id=?', (review_id,)).fetchone()
            if not review or review['state'] not in {'queued', 'claimed'}:
                raise LedgerError('precheck requires a current pending review')
            task = self._task(db, {'project': review['project'], 'workflow': review['workflow'], 'task': review['task']})
            if (task['revision'], task['attempt'], task['status']) != (review['revision'], review['attempt'], 'reported'):
                raise LedgerError('task changed during precheck')
            if text(reviewer) in {owner, task['thread']}:
                raise LedgerError('precheck must identify a separate reviewer')
            root = Path(self._project(db, review['project'])['root'])
            verify_evidence(root, json.loads(review['manifest']))
            proof = evidence(root, source)
            if proof['path'] in {m['path'] for m in json.loads(review['manifest'])}:
                raise LedgerError('precheck must be independent of executor evidence')
            db.execute('INSERT INTO prechecks VALUES(?,?,?,?,?)', (review_id, reviewer, encode(proof), text(note), now()))
            self._audit(db, 'review.prechecked', review['project'], {'review_id': review_id, 'reviewer': reviewer, 'evidence': proof})

    def next_review(self: Ledger, owner: str, epoch: int, review_id: str | None = None) -> dict[str, object]:
        """Claim is idempotent; a focus failure leaves a resumable claim, never accepts work."""
        review = self.claim(owner, epoch, review_id)
        packet = self.focus(review['project'], owner, epoch, workflow=review['workflow'], task=review['task'])
        with self.connection() as db:
            prechecks = checked_prechecks(self, db, review['id'])
        return {'review': {k: v for k, v in review.items() if k != 'manifest'}, 'context': packet,
                'prechecks': prechecks, 'next': 'Read the task card, complete report and evidence; prechecks never replace the final verdict.'}
