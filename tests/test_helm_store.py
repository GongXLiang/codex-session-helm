from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from helm_store import Ledger, LedgerError, TaskKey, now, publish, read_json


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / "ledger.sqlite")
        self.ledger.initialize("supervisor", active_limit=6)
        # Regression coverage for existing schema-1 ledgers; strict v2 gates have their own tests.
        with self.ledger.connection(write=True) as db:
            for table in ("contexts", "focus", "checkpoints", "decisions"):
                db.execute("DROP TABLE " + table)
            db.execute("UPDATE meta SET value='1' WHERE key='schema'")
        self.roots: dict[str, Path] = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def register(self, project: str, *, dependent: bool = False, root: Path | None = None) -> TaskKey:
        root = root or self.base / project
        root.mkdir(exist_ok=True)
        (root / "plan.md").write_text("Test plan", encoding="utf-8")
        self.roots[project] = root
        task = {"id": "T01", "title": "Independent task", "version": 1, "status": "planned",
                "depends_on": [], "workspace": "test", "write_paths": ["output"]}
        tasks = [task]
        if dependent:
            tasks.append({**task, "id": "T02", "depends_on": [{"id": "T01", "version": 1}], "write_paths": ["summary"]})
        self.ledger.register({"id": project, "title": project, "root": str(root), "host": "local",
            "goal": "Isolated test", "plan": "plan.md", "workflow": {"schema_version": 1,
            "workflow_id": "work", "updated_at": now(), "concurrency_limit": 6,
            "case_sensitive_paths": True, "tasks": tasks}}, "supervisor", 1)
        return {"project": project, "workflow": "work", "task": "T01"}

    def dispatch(self, key: TaskKey) -> dict[str, object]:
        intent = self.ledger.prepare(key, "supervisor", 1)
        return self.ledger.bind(str(intent["action_id"]), key["project"] + "-worker", "native tool receipt", "supervisor", 1)

    def result(self, key: TaskKey, binding: dict[str, object]) -> Path:
        root = self.roots[key["project"]]
        (root / "output").mkdir(exist_ok=True)
        (root / "output/report.md").write_text("Report", encoding="utf-8")
        (root / "output/result.txt").write_text("Verified result", encoding="utf-8")
        return publish(root, binding, "RESULT", "ready for review", "output/report.md", ["output/result.txt"])

    def accept(self, key: TaskKey) -> None:
        self.ledger.observe(key, key["project"] + "-worker", "idle", "checked thread and no background process", "supervisor", 1)
        claim = self.ledger.claim("supervisor", 1)
        (self.roots[key["project"]] / "review.md").write_text("Independently checked", encoding="utf-8")
        self.ledger.decide(str(claim["id"]), str(claim["token"]), "accepted", "review.md", "passed", "supervisor", 1)

    def test_three_projects_same_task_id_and_context_are_isolated(self) -> None:
        for project in ["data", "code", "writing"]:
            key = self.register(project)
            self.result(key, self.dispatch(key))
        self.assertEqual(self.ledger.scan()["imported"], 3)
        self.assertEqual(len(self.ledger.snapshot()["reviews"]), 3)
        context = self.ledger.context({"project": "data", "workflow": "work", "task": "T01"})
        self.assertEqual(context["project"]["id"], "data")
        self.assertNotIn(str(self.roots["writing"]), json.dumps(context))

    def test_duplicate_concurrent_delivery_enqueues_one_review(self) -> None:
        key = self.register("data")
        event = read_json(self.result(key, self.dispatch(key)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.ledger.ingest("data", event), range(4)))
        self.assertEqual(results.count("applied"), 1)
        self.assertEqual(results.count("duplicate"), 3)
        self.assertEqual(len(self.ledger.snapshot()["reviews"]), 1)
        event["summary"] = "a different result using the same ID"
        with self.assertRaisesRegex(LedgerError, "collision"):
            self.ledger.ingest("data", event)

    def test_result_does_not_release_dependency_until_verified_acceptance(self) -> None:
        key = self.register("data", dependent=True)
        downstream = {**key, "task": "T02"}
        self.result(key, self.dispatch(key))
        self.ledger.scan()
        with self.assertRaisesRegex(LedgerError, "dependency"):
            self.ledger.prepare(downstream, "supervisor", 1)
        self.accept(key)
        self.ledger.prepare(downstream, "supervisor", 1)

    def test_changed_artifact_cannot_be_accepted(self) -> None:
        key = self.register("data")
        self.result(key, self.dispatch(key))
        self.ledger.scan()
        (self.roots["data"] / "output/result.txt").write_text("changed after report", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "evidence changed"):
            self.accept(key)
        self.assertEqual(self.ledger.snapshot()["tasks"][0]["status"], "reported")

    def test_revision_during_review_fences_old_verdict_and_old_event(self) -> None:
        key = self.register("data")
        binding = self.dispatch(key)
        self.result(key, binding)
        self.ledger.scan()
        claim = self.ledger.claim("supervisor", 1)
        spec = self.ledger.context(key)["task"]
        spec["version"] = 2
        self.ledger.revise(key, spec, "user revised the scope", "supervisor", 1)
        with self.assertRaisesRegex(LedgerError, "claim"):
            self.ledger.decide(str(claim["id"]), str(claim["token"]), "accepted", "review.md", "old verdict", "supervisor", 1)
        late = read_json(publish(self.roots["data"], binding, "MILESTONE", "old attempt late update"))
        self.assertEqual(self.ledger.ingest("data", late), "stale")
        self.assertEqual(self.ledger.context(key)["revision"], 2)

    def test_handoff_recovers_review_and_rejects_old_supervisor(self) -> None:
        key = self.register("data")
        self.result(key, self.dispatch(key))
        self.ledger.scan()
        self.ledger.claim("supervisor", 1)
        self.assertEqual(self.ledger.handoff("new-supervisor", "supervisor", 1, "explicit handoff"), 2)
        with self.assertRaisesRegex(LedgerError, "ownership"):
            self.ledger.claim("supervisor", 1)
        recovered = Ledger(self.ledger.path).claim("new-supervisor", 2)
        self.assertEqual(recovered["state"], "claimed")

    def test_wrong_project_report_is_quarantined_not_applied(self) -> None:
        key = self.register("data")
        self.register("code")
        event = read_json(self.result(key, self.dispatch(key)))
        event["project"] = "code"
        path = self.roots["data"] / ".session-helm/outbox/misrouted.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        counts = self.ledger.scan()
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(len(self.ledger.snapshot()["reviews"]), 1)

    def test_conflicting_scopes_across_nested_projects_are_blocked(self) -> None:
        parent = self.register("parent")
        nested = self.roots["parent"] / "output"
        child = self.register("child", root=nested)
        self.dispatch(parent)
        with self.assertRaisesRegex(LedgerError, "write scope"):
            self.ledger.prepare(child, "supervisor", 1)

    def test_missing_ledger_does_not_create_an_empty_database(self) -> None:
        path = self.base / "typo.sqlite"
        with self.assertRaises(LedgerError):
            Ledger(path).snapshot()
        self.assertFalse(path.exists())

    def test_first_version_refuses_to_mix_hosts_in_one_ledger(self) -> None:
        key = self.register("local-project")
        other = self.base / "remote-project"
        other.mkdir()
        (other / "plan.md").write_text("separate host", encoding="utf-8")
        config = {"id":"remote-project","title":"Remote","root":str(other),"host":"another-host",
            "goal":"unsupported remote execution","plan":"plan.md","workflow":self.ledger.export(key["project"],"work")}
        with self.assertRaisesRegex(LedgerError, "one host"):
            self.ledger.register(config,"supervisor",1)

    def test_only_one_review_can_be_claimed_and_release_preserves_queue(self) -> None:
        for project in ("a", "b"):
            key = self.register(project)
            self.result(key, self.dispatch(key))
        self.ledger.scan()
        first = self.ledger.claim("supervisor", 1)
        self.assertEqual(self.ledger.claim("supervisor", 1)["token"], first["token"])
        other = next(r for r in self.ledger.snapshot()["reviews"] if r["id"] != first["id"])
        with self.assertRaisesRegex(LedgerError, "current review"):
            self.ledger.claim("supervisor", 1, other["id"])
        self.ledger.release(first["id"], first["token"], "supervisor", 1)
        self.assertEqual(self.ledger.claim("supervisor", 1, other["id"])["id"], other["id"])

    def test_review_backlog_blocks_further_dispatch(self) -> None:
        for project in ("a", "b", "c", "d"):
            key = self.register(project)
            self.result(key, self.dispatch(key))
        self.ledger.scan()
        key = self.register("e")
        with self.assertRaisesRegex(LedgerError, "backlog"):
            self.ledger.prepare(key, "supervisor", 1)

    def test_upstream_revision_invalidates_accepted_downstream(self) -> None:
        key = self.register("data", dependent=True)
        self.result(key, self.dispatch(key))
        self.ledger.scan()
        self.accept(key)
        downstream = {**key, "task": "T02"}
        self.result(downstream, self.dispatch(downstream))
        self.ledger.scan()
        self.accept(downstream)
        spec = self.ledger.context(key)["task"]
        self.ledger.revise(key, {**spec, "version": 2}, "new scope", "supervisor", 1)
        task = next(t for t in self.ledger.snapshot()["tasks"] if t["id"] == "T02")
        self.assertEqual(task["effective_status"], "needs_recheck")

    def test_cancel_requires_undelivered_intent_and_rejects_late_bind(self) -> None:
        key = self.register("data")
        intent = self.ledger.prepare(key, "supervisor", 1)
        self.ledger.cancel_dispatch(intent["action_id"], "native history confirms no dispatch", "supervisor", 1)
        with self.assertRaisesRegex(LedgerError, "cancelled"):
            self.ledger.bind(intent["action_id"], "worker", "late", "supervisor", 1)
        next_intent = self.ledger.prepare(key, "supervisor", 1)
        binding = self.ledger.bind(next_intent["action_id"], "worker", "receipt", "supervisor", 1)
        sequence = self.ledger.sequence()
        self.assertEqual(self.ledger.bind(next_intent["action_id"], "worker", "receipt", "supervisor", 1), binding)
        self.assertEqual(self.ledger.sequence(), sequence)
        with self.assertRaisesRegex(LedgerError, "undelivered"):
            self.ledger.cancel_dispatch(next_intent["action_id"], "wrong", "supervisor", 1)

    def test_concurrent_publish_cannot_overwrite_event(self) -> None:
        key = self.register("data")
        binding = self.dispatch(key)
        def attempt(index: int) -> bool:
            try:
                publish(self.roots["data"], binding, "MILESTONE", f"worker {index}", event_id="same-id")
                return True
            except (LedgerError, FileExistsError):
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(attempt, range(4))).count(True), 1)
        self.assertEqual(self.ledger.scan()["imported"], 1)

    def test_unreadable_and_oversized_outbox_do_not_block_other_project(self) -> None:
        key = self.register("data")
        broken = self.result(key, self.dispatch(key))
        key2 = self.register("code")
        self.result(key2, self.dispatch(key2))
        huge = self.roots["data"] / ".session-helm/outbox/huge.json"
        huge.write_bytes(b"x" * (256 * 1024 + 1))
        from helm_store import digest
        def conditional_digest(path: Path) -> str:
            if path == broken:
                raise PermissionError("locked test event")
            return digest(path)
        with patch("helm_store.digest", side_effect=conditional_digest):
            counts = self.ledger.scan()
            self.assertEqual(counts["rejected"], 2)
            self.assertEqual(counts["imported"], 1)
            sequence = self.ledger.sequence()
            self.ledger.scan()
            self.assertEqual(self.ledger.sequence(), sequence)


if __name__ == "__main__":
    unittest.main()
