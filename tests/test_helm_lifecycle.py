from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_helm_context as fixtures
from helm_store import Ledger, LedgerError, encode, publish, read_json
import helm_context
import helm_lifecycle


class LifecycleTests(unittest.TestCase):
    setUp = fixtures.SupervisorContextTests.setUp
    tearDown = fixtures.SupervisorContextTests.tearDown
    capsule = staticmethod(fixtures.SupervisorContextTests.capsule)
    key = staticmethod(fixtures.SupervisorContextTests.key)
    leave = fixtures.SupervisorContextTests.leave
    submit = fixtures.SupervisorContextTests.submit

    def task(self, name: str = 'T02') -> dict[str, object]:
        return {'id': name, 'title': name, 'version': 1, 'status': 'planned', 'depends_on': [], 'workspace': 'cards', 'write_paths': ['out/' + name]}

    def accept_cards(self) -> None:
        self.submit('cards')
        review = self.ledger.next_review('chief', 1)['review']
        (self.base / 'cards/review.md').write_text('Independent review: passed', encoding='utf-8')
        self.ledger.decide(review['id'], review['token'], 'accepted', 'review.md', 'checked', 'chief', 1)
        self.ledger.focus('cards', 'chief', 1)

    def test_append_generation_validation_and_rollback(self) -> None:
        self.ledger.focus('cards', 'chief', 1)
        before = self.ledger.list_tasks('cards')['total']
        for task in (self.task('T01'), {**self.task(), 'depends_on': [{'id': 'T02', 'version': 1}]}, {**self.task(), 'write_paths': ['../escape']}):
            with self.assertRaises(ValueError):
                self.ledger.append_tasks('cards', 'main', [task], 1, 'follow-up', 'chief', 1)
        self.assertEqual(self.ledger.list_tasks('cards')['total'], before)
        self.ledger.append_tasks('cards', 'main', [self.task()], 1, 'new user goal', 'chief', 1)
        self.ledger.focus('cards', 'chief', 1)
        with self.assertRaisesRegex(LedgerError, 'generation'):
            self.ledger.append_tasks('cards', 'main', [self.task('T03')], 1, 'stale', 'chief', 1)
        self.ledger.append_tasks('cards', 'new-workflow', [self.task('T01')], 0, 'different workflow', 'chief', 1)
        self.assertEqual(self.ledger.list_tasks('cards')['total'], 3)

    def test_archive_restore_retains_history_and_allows_growth(self) -> None:
        self.ledger.focus('cards', 'chief', 1)
        with self.assertRaises(LedgerError):
            self.ledger.archive_workflow('cards', 'main', 1, 'too early', 'chief', 1)
        self.accept_cards()
        self.ledger.archive_workflow('cards', 'main', 1, 'release complete', 'chief', 1)
        self.assertEqual(self.ledger.list_tasks('cards')['total'], 0)
        self.assertEqual(self.ledger.list_tasks('cards', include_archived=True)['total'], 1)
        self.assertEqual(self.ledger.export('cards', 'main')['tasks'][0]['status'], 'accepted')
        self.ledger.focus('cards', 'chief', 1)
        with self.assertRaisesRegex(LedgerError, 'restore'):
            self.ledger.append_tasks('cards', 'main', [self.task()], 2, 'more', 'chief', 1)
        # Restore must allow repairing evidence that changed after archiving.
        (self.base / 'cards/report.md').write_text('changed', encoding='utf-8')
        self.ledger.archive_workflow('cards', 'main', 2, 'reopen to repair', 'chief', 1, restore=True)
        self.ledger.focus('cards', 'chief', 1)
        self.ledger.append_tasks('cards', 'main', [self.task()], 3, 'new work', 'chief', 1)
        self.assertEqual(self.ledger.list_tasks('cards')['total'], 2)

    def test_large_context_pagination_and_delta_are_bounded(self) -> None:
        for batch in range(10):
            self.ledger.focus('cards', 'chief', 1)
            self.ledger.append_tasks('cards', 'main', [self.task(f'X{i:04}') for i in range(batch*100, (batch+1)*100)], batch+1, 'scale fixture', 'chief', 1)
        full = self.ledger.focus('cards', 'chief', 1)
        self.assertEqual(full['task_counts']['relevant'], 1001)
        self.assertEqual(len(full['tasks']), 12)
        self.assertLess(len(encode(full)), 24000)
        page = self.ledger.list_tasks('cards', offset=990, limit=50)
        self.assertEqual(len(page['tasks']), 11)
        self.assertIsNone(page['next_offset'])
        diff = self.ledger.focus('cards', 'chief', 1, delta=True)
        self.assertFalse(diff['changed'])
        self.assertLess(len(encode(diff)), len(encode(full)) // 4)

    def test_progress_and_unrelated_result_keep_task_focus_but_revision_invalidates(self) -> None:
        self.ledger.focus('cards', 'chief', 1)
        self.ledger.append_tasks('cards', 'main', [self.task()], 1, 'parallel task', 'chief', 1)
        bindings = []
        for task in ('T01','T02'):
            key = {**self.key('cards'), 'task': task}
            self.ledger.focus('cards', 'chief', 1, workflow='main', task=task)
            intent = self.ledger.prepare(key, 'chief', 1)
            bindings.append(self.ledger.bind(intent['action_id'], 'worker-' + task, 'receipt', 'chief', 1))
        packet = self.ledger.focus('cards', 'chief', 1, workflow='main', task='T01')
        self.ledger.ingest('cards', read_json(publish(self.base/'cards', bindings[0], 'MILESTONE', 'ordinary progress')))
        (self.base/'cards/other.md').write_text('Other result', encoding='utf-8')
        self.ledger.ingest('cards', read_json(publish(self.base/'cards', bindings[1], 'RESULT', 'Done', 'other.md')))
        with self.ledger.connection() as db:
            helm_context.require_focus(self.ledger, db, 'cards', 'chief', 1, self.key('cards'))
            with self.assertRaisesRegex(LedgerError, 'exact task'):
                helm_context.require_focus(self.ledger, db, 'cards', 'chief', 1, {**self.key('cards'), 'task': 'T02'})
        self.assertEqual(self.ledger.focus('cards', 'chief', 1, workflow='main', task='T01')['fingerprint'], packet['fingerprint'])
        self.ledger.revise(self.key('cards'), {**self.task('T01'), 'version': 2}, 'changed scope', 'chief', 1)
        with self.ledger.connection() as db:
            with self.assertRaisesRegex(LedgerError, 'context changed'):
                helm_context.require_focus(self.ledger, db, 'cards', 'chief', 1, self.key('cards'))

    def test_target_focus_includes_dependencies(self) -> None:
        packet = self.ledger.focus('money', 'chief', 1, workflow='main', task='T02')
        self.assertEqual({t['id'] for t in packet['tasks']}, {'T01', 'T02'})
        with self.assertRaisesRegex(LedgerError, 'dependency'):
            self.ledger.prepare({**self.key('money'), 'task':'T02'}, 'chief', 1)

    def test_precheck_is_advisory_and_changed_evidence_is_flagged(self) -> None:
        self.submit('cards')
        review = self.ledger.snapshot()['reviews'][0]
        proof = self.base/'cards/precheck.md'
        proof.write_text('Reviewer independently tested', encoding='utf-8')
        with self.assertRaisesRegex(LedgerError, 'separate reviewer'):
            self.ledger.record_precheck(review['id'], 'cards-worker', 'precheck.md', 'test', 'chief', 1)
        self.ledger.record_precheck(review['id'], 'reviewer', 'precheck.md', 'checked', 'chief', 1)
        result = self.ledger.next_review('chief', 1)
        self.assertTrue(result['prechecks'][0]['valid'])
        self.assertEqual(result['review']['state'], 'claimed')
        self.assertEqual(self.ledger.list_tasks('cards')['tasks'][0]['status'], 'reported')
        original = (self.base/'cards/report.md').read_text(encoding='utf-8')
        (self.base/'cards/report.md').write_text('changed result', encoding='utf-8')
        self.assertFalse(self.ledger.next_review('chief', 1)['prechecks'][0]['valid'])
        (self.base/'cards/report.md').write_text(original, encoding='utf-8')
        proof.write_text('modified', encoding='utf-8')
        self.assertFalse(self.ledger.next_review('chief', 1)['prechecks'][0]['valid'])
        self.ledger.revise(self.key('cards'), {**self.task('T01'), 'version': 2}, 'rework', 'chief', 1)
        with self.ledger.connection() as db:
            self.assertEqual(helm_lifecycle.checked_prechecks(self.ledger, db, review['id']), [])

    def test_delivery_gate_separates_gui_and_task_acceptance(self) -> None:
        self.accept_cards()
        self.ledger.set_gate('cards', 'gui', 'Browser interaction', 'pending', 'Not tested', None, [], 'chief', 1)
        self.assertEqual(self.ledger.snapshot()['deliveries'][1]['state'], 'pending')
        with self.assertRaisesRegex(LedgerError, 'separate'):
            self.ledger.set_gate('cards','gui','Browser interaction','passed','checked','report.md',[self.key('cards')],'chief',1)
        self.ledger.set_gate('cards','gui','Browser interaction','passed','checked','review.md',[self.key('cards')],'chief',1)
        self.assertEqual(self.ledger.snapshot()['deliveries'][1]['state'], 'passed')
        self.ledger.archive_workflow('cards','main',1,'complete','chief',1)
        (self.base/'cards/review.md').write_text('changed review', encoding='utf-8')
        gate = self.ledger.snapshot()['deliveries'][1]
        self.assertEqual(gate['state'], 'pending')
        self.assertEqual(gate['gates'][0]['state'], 'needs_recheck')

    def test_incremental_scan_edit_restart_periodic_and_explicit_verification(self) -> None:
        self.ledger.focus('cards','chief',1)
        intent = self.ledger.prepare(self.key('cards'),'chief',1)
        binding = self.ledger.bind(intent['action_id'],'cards-worker','receipt','chief',1)
        path = publish(self.base/'cards',binding,'MILESTONE','progress')
        self.ledger.scan()
        self.ledger.scan()
        self.assertEqual(self.ledger.last_scan_stats, {'hashed':0, 'cached':1})
        self.ledger.scan(verify=True)
        self.assertEqual(self.ledger.last_scan_stats['hashed'],1)
        key = str(path)
        sig, timestamp = self.ledger._scan_cache[key]
        self.ledger._scan_cache[key] = (sig, timestamp-61)
        self.ledger.scan()
        self.assertEqual(self.ledger.last_scan_stats['hashed'],1)
        fresh = Ledger(self.ledger.path)
        fresh.scan()
        self.assertEqual(fresh.last_scan_stats['hashed'],1)
        event = read_json(path); event['summary'] = 'tampered event'
        path.write_text(encode(event),encoding='utf-8')
        self.assertEqual(self.ledger.scan()['rejected'],1)

    def test_schema2_upgrade_retains_evidence_and_backup(self) -> None:
        self.accept_cards()
        with self.ledger.connection(write=True) as db:
            for table in ('workflow_state','context_views','delivery_gates','prechecks'):
                db.execute('DROP TABLE ' + table)
            db.execute("UPDATE meta SET value='2' WHERE key='schema'")
        backup = self.base/'schema2.sqlite'
        self.ledger.upgrade(backup,'chief',1)
        self.assertEqual(self.ledger.list_tasks('cards')['tasks'][0]['status'],'accepted')
        db = sqlite3.connect(backup)
        try:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0],'2')
        finally:
            db.close()
        with self.assertRaisesRegex(LedgerError, 'focus'):
            self.ledger.append_tasks('cards','main',[self.task()],1,'new work','chief',1)

    def test_delivery_rechecks_upstream_evidence_and_new_work(self) -> None:
        self.accept_cards()
        spec={**self.task(), 'depends_on':[{'id':'T01','version':1}]}
        self.ledger.append_tasks('cards','main',[spec],1,'second release','chief',1)
        self.ledger.focus('cards','chief',1,workflow='main',task='T02')
        key={**self.key('cards'),'task':'T02'}
        action=self.ledger.prepare(key,'chief',1)
        binding=self.ledger.bind(action['action_id'],'new-worker','receipt','chief',1)
        root=self.base/'cards'
        (root/'second.md').write_text('second artifact',encoding='utf-8')
        self.ledger.ingest('cards',read_json(publish(root,binding,'RESULT','done','second.md')))
        self.ledger.observe(key,'new-worker','idle','native idle','chief',1)
        record=self.ledger.next_review('chief',1)['review']
        (root/'second-review.md').write_text('independent acceptance',encoding='utf-8')
        self.ledger.decide(record['id'],record['token'],'accepted','second-review.md','checked','chief',1)
        self.ledger.focus('cards','chief',1)
        self.ledger.set_gate('cards','release','Release','passed','checked','second-review.md',[key],'chief',1)
        self.assertEqual(self.ledger.snapshot()['deliveries'][1]['state'],'passed')
        self.ledger.append_tasks('cards','main',[self.task('T03')],2,'next need','chief',1)
        self.assertEqual(self.ledger.snapshot()['deliveries'][1]['state'],'pending')
        (root/'report.md').write_text('tampered upstream',encoding='utf-8')
        self.assertEqual(self.ledger.snapshot()['deliveries'][1]['gates'][0]['state'],'needs_recheck')


if __name__ == '__main__':
    unittest.main()
