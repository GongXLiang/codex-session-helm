"""Behavior checks for user preference, task assessment and host boundaries."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_router as router

CATALOG = (
    router.Model("small", ("focused",), ("low", "high"), "high"),
    router.Model("workhorse", ("general",), ("low", "medium", "high", "xhigh"), "medium"),
    router.Model("reasoner", ("deep",), ("low", "medium", "high", "xhigh", "max", "ultra"), "low"),
    router.Model("visual", ("visual",), ("low", "medium", "high"), "medium"),
)


def request(assessment: router.Assessment | None = None) -> router.Request:
    return router.Request(assessment or router.Assessment(0, 0, 0, 0), CATALOG,
                          "workhorse", "allowed", True)


class RoutingTests(unittest.TestCase):
    def test_clear_repeatable_task_uses_focused_model_baseline(self) -> None:
        decision = router.route(request())
        self.assertEqual(decision.overrides, {"model": "small", "thinking": "high"})
        self.assertFalse(decision.runtime_verified)

    def test_normal_implementation_uses_workhorse_medium(self) -> None:
        result = router.route(request(router.Assessment(1, 1, 0, 0)))
        self.assertEqual(result.overrides, {"model": "workhorse", "thinking": "medium"})

    def test_short_but_high_consequence_task_uses_deep_model(self) -> None:
        self.assertEqual(router.route(request(router.Assessment(0, 0, 2, 0))).overrides,
                         {"model": "reasoner", "thinking": "high"})

    def test_difficult_verification_prevents_lightweight_routing(self) -> None:
        self.assertEqual(router.route(request(router.Assessment(0, 0, 0, 2))).role, "deep")

    def test_combined_uncertainty_selects_complex_profile(self) -> None:
        self.assertEqual(router.route(request(router.Assessment(2, 2, 0, 1))).level, "complex")

    def test_visual_work_uses_visual_role_unless_high_consequence(self) -> None:
        self.assertEqual(router.route(request(router.Assessment(0, 0, 0, 0, "visual"))).target["model"], "visual")
        self.assertEqual(router.route(request(router.Assessment(0, 0, 2, 0, "visual"))).target["model"], "reasoner")

    def test_explicit_model_and_effort_win_over_complexity(self) -> None:
        result = router.route(replace(request(router.Assessment(2, 2, 2, 2)), user_model="workhorse", user_effort="low"))
        self.assertEqual(result.overrides, {"model": "workhorse", "thinking": "low"})

    def test_only_model_is_fixed_effort_still_adapts(self) -> None:
        result = router.route(replace(request(router.Assessment(2, 2, 2, 2)), user_model="workhorse"))
        self.assertEqual(result.overrides["thinking"], "high")

    def test_only_effort_is_fixed_model_still_adapts(self) -> None:
        result = router.route(replace(request(router.Assessment(2, 2, 2, 2)), user_effort="low"))
        self.assertEqual(result.overrides, {"model": "reasoner", "thinking": "low"})

    def test_only_effort_excludes_incompatible_automatic_candidate(self) -> None:
        result = router.route(replace(request(), user_effort="medium"))
        self.assertEqual(result.overrides, {"model": "workhorse", "thinking": "medium"})

    def test_inherit_mode_preserves_both_settings(self) -> None:
        self.assertEqual(router.route(replace(request(), mode="inherit")).overrides, {})

    def test_unavailable_explicit_model_is_not_silently_replaced(self) -> None:
        with self.assertRaises(router.RoutingError):
            router.route(replace(request(), user_model="not-installed"))

    def test_unsupported_explicit_effort_is_not_silently_replaced(self) -> None:
        with self.assertRaises(router.RoutingError):
            router.route(replace(request(), user_model="small", user_effort="medium"))

    def test_create_restriction_omits_model_and_recalculates_effort(self) -> None:
        result = router.route(replace(request(), model_policy="explicit_only"))
        self.assertEqual(result.recommended, {"model": "small", "thinking": "high"})
        self.assertEqual(result.overrides, {"thinking": "medium"})
        self.assertEqual(result.target["model"], "workhorse")

    def test_unknown_default_omits_model_specific_effort(self) -> None:
        result = router.route(replace(request(), default_model=None, model_policy="explicit_only"))
        self.assertEqual(result.overrides, {})
        self.assertIsNone(result.target["model"])

    def test_explicit_model_is_allowed_by_explicit_only_host(self) -> None:
        result = router.route(replace(request(), model_policy="explicit_only", user_model="reasoner"))
        self.assertEqual(result.overrides, {"model": "reasoner", "thinking": "low"})

    def test_host_cannot_apply_explicit_model_or_effort(self) -> None:
        for case in (replace(request(), model_policy="unsupported", user_model="small"),
                     replace(request(), thinking_allowed=False, user_effort="high"),
                     replace(request(), default_model=None, model_policy="explicit_only", user_effort="high")):
            with self.subTest(case=case), self.assertRaises(router.RoutingError):
                router.route(case)

    def test_automatic_effort_respects_ceiling(self) -> None:
        result = router.route(replace(request(router.Assessment(2, 2, 2, 2)), max_auto_effort="medium"))
        self.assertEqual(result.overrides["thinking"], "medium")

    def test_supported_effort_fallback_stays_below_ceiling(self) -> None:
        result = router.route(replace(request(), max_auto_effort="medium"))
        self.assertEqual(result.overrides["thinking"], "low")

    def test_explicit_max_preserved_but_never_automatically_selected(self) -> None:
        self.assertEqual(router.route(replace(request(), user_model="reasoner", user_effort="max")).overrides["thinking"], "max")
        for a in range(3):
            for b in range(3):
                for c in range(3):
                    for d in range(3):
                        decision = router.route(request(router.Assessment(a, b, c, d)))
                        self.assertNotIn(decision.overrides.get("thinking"), ("max", "ultra"))

    def test_model_allowlist_is_preserved(self) -> None:
        result = router.route(replace(request(router.Assessment(2, 2, 2, 2)), allowed_model_ids=("workhorse",)))
        self.assertEqual(result.target["model"], "workhorse")
        with self.assertRaises(router.RoutingError):
            router.route(replace(request(), allowed_model_ids=("small",), model_policy="explicit_only"))

    def test_missing_preferred_role_preserves_known_default(self) -> None:
        result = router.route(replace(request(), models=CATALOG[1:]))
        self.assertEqual(result.target["model"], "workhorse")

    def test_empty_catalog_is_honest_about_unknown_runtime(self) -> None:
        result = router.route(replace(request(), models=(), default_model=None))
        self.assertEqual(result.overrides, {})
        self.assertIsNone(result.target["model"])

    def test_invalid_catalog_and_policy_are_rejected(self) -> None:
        cases = (replace(request(), models=CATALOG + CATALOG),
                 replace(request(), model_policy="maybe"),
                 replace(request(), allowed_model_ids=()),
                 replace(request(), default_model="unknown"),
                 replace(request(), max_auto_effort="ultra"),
                 replace(request(), assessment=router.Assessment(True, 0, 0, 0)),
                 replace(request(), mode="inherit", user_model="small"),
                 replace(request(), models=(replace(CATALOG[0], baseline_effort="ultra"),), default_model="small"))
        for case in cases:
            with self.subTest(case=case), self.assertRaises(router.RoutingError):
                router.route(case)

    def test_auto_model_change_cannot_inherit_an_unbounded_effort(self) -> None:
        model = router.Model("extreme", ("focused",), ("ultra",), "ultra")
        for case in (replace(request(), models=(model,), default_model="extreme"),
                     replace(request(), thinking_allowed=False)):
            with self.subTest(case=case), self.assertRaises(router.RoutingError):
                router.route(case)

    def test_misspelled_user_setting_is_not_ignored(self) -> None:
        root = Path(__file__).resolve().parents[1]
        raw = json.loads((root / "examples" / "model-routing.json").read_text(encoding="utf-8"))
        raw["user_modle"] = "locked-model"
        with self.assertRaises(router.RoutingError):
            router.parse_request(raw)

    def test_cli_example_is_read_only_and_does_not_claim_execution(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = root / "examples" / "model-routing.json"
        before = source.read_bytes()
        completed = subprocess.run([sys.executable, "-B", str(root / "scripts" / "model_router.py"), str(source)],
                                   capture_output=True, check=True, encoding="utf-8")
        result = json.loads(completed.stdout)
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(result["overrides"], {})
        self.assertEqual(source.read_bytes(), before)

    def test_cli_rejects_invalid_input_without_success_output(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"schema_version": 1}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(root / "scripts" / "model_router.py"), str(path)],
                                    capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
