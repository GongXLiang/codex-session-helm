from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from helm_store import Ledger, LedgerError, now, publish, read_json


class SupervisorContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / "ledger.sqlite")
        self.ledger.initialize("chief", active_limit=6, review_limit=4)
        for name, rule in (("money", "amount is integer cents"), ("archive", "valid means SHA-256 matches"), ("cards", "due means local calendar date")):
            root = self.base / name
            root.mkdir()
            (root / "plan.md").write_text(rule, encoding="utf-8")
            tasks = [{"id": "T01", "title": "First release", "version": 1, "status": "planned", "depends_on": [], "workspace": name, "write_paths": ["v1"]}]
            if name == "money":
                tasks.append({**tasks[0], "id": "T02", "title": "Second release", "write_paths": ["v2"], "depends_on": [{"id": "T01", "version": 1}]})
            self.ledger.register({"id": name, "title": name, "root": str(root), "host": "local", "goal": rule,
                "plan": "plan.md", "workflow": {"schema_version": 1, "workflow_id": "main", "updated_at": now(),
                "concurrency_limit": 2, "case_sensitive_paths": True, "tasks": tasks}}, "chief", 1)
            self.ledger.save_context(name, self.capsule(name, rule), 0, "chief", 1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def key(project: str) -> dict[str, str]:
        return {"project": project, "workflow": "main", "task": "T01"}

    @staticmethod
    def capsule(project: str, rule: str) -> dict[str, object]:
        return {"project": project, "goal": rule, "rules": [rule], "definitions": [rule],
            "decisions": [], "open_questions": [], "next_step": "Implement first release", "sources": ["plan.md"]}

    def leave(self, project: str) -> None:
        self.ledger.checkpoint(project, "Current round saved", "Review latest report", [], "chief", 1)

    def submit(self, project: str) -> dict[str, object]:
        self.ledger.focus(project, "chief", 1)
        intent = self.ledger.prepare(self.key(project), "chief", 1)
        binding = self.ledger.bind(intent["action_id"], project + "-worker", "real receipt reference", "chief", 1)
        root = self.base / project
        (root / "report.md").write_text("Complete report", encoding="utf-8")
        event = read_json(publish(root, binding, "RESULT", "Ready", "report.md"))
        self.ledger.ingest(project, event)
        self.ledger.observe(self.key(project), project + "-worker", "idle", "native observation", "chief", 1)
        self.leave(project)
        return event

    def test_wrong_focus_unsaved_switch_and_stale_version_are_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "focus"):
            self.ledger.prepare(self.key("money"), "chief", 1)
        packet = self.ledger.focus("money", "chief", 1)
        self.assertNotIn("SHA-256", json.dumps(packet))
        self.assertNotIn(str(self.base / "cards"), json.dumps(packet))
        with self.assertRaisesRegex(LedgerError, "checkpoint"):
            self.ledger.focus("archive", "chief", 1)
        with self.assertRaisesRegex(LedgerError, "focus"):
            self.ledger.prepare(self.key("archive"), "chief", 1)
        self.ledger.save_context("money", self.capsule("money", "new rule"), 1, "chief", 1)
        with self.assertRaisesRegex(LedgerError, "context changed"):
            self.ledger.prepare(self.key("money"), "chief", 1)
        self.ledger.focus("money", "chief", 1)
        self.ledger.prepare(self.key("money"), "chief", 1)
        self.leave("money")
        self.assertEqual(self.ledger.focus("archive", "chief", 1)["context_version"], 1)

    def test_changed_plan_foreign_evidence_and_oversized_capsule_fail_closed(self) -> None:
        self.ledger.focus("money", "chief", 1)
        (self.base / "money/plan.md").write_text("changed out of band", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "evidence changed"):
            self.ledger.prepare(self.key("money"), "chief", 1)
        capsule = self.capsule("money", "fixed rule")
        capsule["sources"] = ["../archive/plan.md"]
        with self.assertRaises(LedgerError):
            self.ledger.save_context("money", capsule, 1, "chief", 1)
        capsule["sources"] = ["plan.md"]
        capsule["goal"] = "x" * 1201
        with self.assertRaisesRegex(LedgerError, "exceeds"):
            self.ledger.save_context("money", capsule, 1, "chief", 1)

    def test_result_invalidates_loaded_packet_and_cross_project_verdict_is_blocked(self) -> None:
        event = self.submit("money")
        claim = self.ledger.claim("chief", 1)
        (self.base / "money/review.md").write_text("independent check", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "focus"):
            self.ledger.decide(claim["id"], claim["token"], "accepted", "review.md", "pass", "chief", 1)
        packet = self.ledger.focus("money", "chief", 1)
        self.assertEqual(packet["tasks"][0]["status"], "reported")
        self.ledger.decide(claim["id"], claim["token"], "accepted", "review.md", "pass", "chief", 1)
        self.assertEqual(self.ledger.ingest("money", event), "duplicate")
        self.leave("money")
        overview = self.ledger.portfolio()
        self.assertNotIn("Complete report", json.dumps(overview))
        self.assertEqual(next(p for p in overview["projects"] if p["project"] == "money")["accepted"], 1)

    def test_context_revision_only_invalidates_its_project_and_lost_update_is_rejected(self) -> None:
        before = self.ledger.focus("archive", "chief", 1)["fingerprint"]
        self.ledger.save_context("money", self.capsule("money", "new cents definition"), 1, "chief", 1)
        self.assertEqual(self.ledger.focus("archive", "chief", 1)["fingerprint"], before)
        with self.assertRaisesRegex(LedgerError, "version changed"):
            self.ledger.save_context("money", self.capsule("money", "lost update"), 1, "chief", 1)
        self.leave("archive")
        self.ledger.focus("money", "chief", 1)
        self.ledger.save_context("money", self.capsule("money", "third definition"), 2, "chief", 1)
        self.leave("money")
        packet = self.ledger.focus("money", "chief", 1)
        self.assertEqual(packet["checkpoint"]["context_version"], 2)
        self.assertFalse(packet["checkpoint_is_current"])

    def test_handoff_recovers_checkpoints_but_requires_fresh_focus(self) -> None:
        self.submit("money")
        old = self.ledger.claim("chief", 1)
        self.ledger.focus("money", "chief", 1)
        self.ledger.handoff("successor", "chief", 1, "resume in fresh chat")
        with self.assertRaisesRegex(LedgerError, "ownership"):
            self.ledger.focus("money", "chief", 1)
        fresh = Ledger(self.ledger.path)
        packet = fresh.focus("money", "successor", 2)
        self.assertEqual(packet["checkpoint"]["summary"], "Current round saved")
        new = fresh.claim("successor", 2)
        self.assertEqual(old["id"], new["id"])
        self.assertNotEqual(old["token"], new["token"])

    def test_queue_impact_rotation_and_aging(self) -> None:
        for project in ("archive", "money", "cards"):
            self.submit(project)
        rows = self.ledger.snapshot()["reviews"]
        self.assertEqual(rows[0]["project"], "money")
        self.assertEqual(rows[0]["unlocks"], 1)
        with self.ledger.connection(write=True) as db:
            db.execute("UPDATE meta SET value='money' WHERE key='last_review_project'")
        self.assertEqual(self.ledger.snapshot()["reviews"][0]["project"], "archive")
        with self.ledger.connection(write=True) as db:
            db.execute("UPDATE reviews SET created_at='2020-01-01T00:00:00+00:00' WHERE project='cards'")
        self.assertEqual(self.ledger.claim("chief", 1)["project"], "cards")

    def test_structured_decision_blocks_dispatch_until_evidenced_answer(self) -> None:
        decision = self.ledger.request_decision(self.key("money"), "Which rule?", "Changes rounding", ["Rule A", "Rule B"], "chief", 1)
        self.ledger.focus("money", "chief", 1)
        with self.assertRaisesRegex(LedgerError, "decision"):
            self.ledger.prepare(self.key("money"), "chief", 1)
        with self.assertRaisesRegex(LedgerError, "missing"):
            self.ledger.resolve_decision(decision, "Rule A", "missing.md", "chief", 1)
        (self.base / "money/answer.md").write_text("User chose Rule A", encoding="utf-8")
        self.ledger.resolve_decision(decision, "Rule A", "answer.md", "chief", 1)
        self.ledger.focus("money", "chief", 1)
        self.ledger.prepare(self.key("money"), "chief", 1)
        self.assertEqual(self.ledger.snapshot()["decisions"], [])

    def test_evidence_view_rejects_unknown_kind_and_modified_report(self) -> None:
        self.submit("money")
        path, checksum = self.ledger.artifact("money", "main", "T01", "report")
        self.assertEqual(path.name, "report.md")
        self.assertEqual(len(checksum), 64)
        with self.assertRaises(LedgerError):
            self.ledger.artifact("money", "main", "T01", "../../plan.md")
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "evidence changed"):
            self.ledger.artifact("money", "main", "T01", "report")

    def test_v1_upgrade_preserves_data_and_produces_consistent_backup(self) -> None:
        with self.ledger.connection(write=True) as db:
            for table in ("focus", "contexts", "checkpoints", "decisions"):
                db.execute("DROP TABLE " + table)
            db.execute("UPDATE meta SET value='1' WHERE key='schema'")
        backup = self.base / "backup.sqlite"
        self.ledger.upgrade(backup, "chief", 1)
        self.assertEqual(len(self.ledger.snapshot()["projects"]), 3)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], "1")
        with self.assertRaisesRegex(LedgerError, "focus"):
            self.ledger.prepare(self.key("money"), "chief", 1)


if __name__ == "__main__":
    unittest.main()
