#!/usr/bin/env python3
"""Tests for scripts/validate_pipeline.py and its agreement with assets/pipeline-schema.json."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_pipeline as vp  # noqa: E402

EXAMPLE = json.loads((REPO_ROOT / "assets" / "example-pipeline.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((REPO_ROOT / "assets" / "pipeline-schema.json").read_text(encoding="utf-8"))
SCRIPT = REPO_ROOT / "scripts" / "validate_pipeline.py"


def tier(plan: dict, tier_id: str) -> dict:
    return next(t for t in plan["tiers"] if t["tier_id"] == tier_id)


class ValidatorCase(unittest.TestCase):
    def plan(self) -> dict:
        return copy.deepcopy(EXAMPLE)

    def assertFails(self, plan: object, fragment: str) -> None:
        issues = vp.validate_pipeline(plan)
        self.assertTrue(any(fragment in i for i in issues), f"expected '{fragment}' in {issues}")


class TestValidPlans(ValidatorCase):
    def test_example_passes(self):
        self.assertEqual(vp.validate_pipeline(self.plan()), [])

    def test_entry_fast_forward_with_satisfied_tiers(self):
        p = self.plan()
        p["entry_tier"] = "L2b"
        p["satisfied_tiers"] = ["L1", "L2a"]
        p["tiers"] = [t for t in p["tiers"] if t["tier_id"] not in ("L1", "L2a")]
        tier(p, "L2b")["rollback_target"] = "L2a"
        self.assertEqual(vp.validate_pipeline(p), [])

    def test_paid_auto_tier_is_allowed_inside_a_user_approved_budget(self):
        p = self.plan()
        p["approved_budget_usd"] = 20
        tier(p, "L4")["gate"] = "auto"
        self.assertEqual(vp.validate_pipeline(p), [])


class TestCostGates(ValidatorCase):
    def test_paid_l4_under_auto_without_budget_fails(self):
        p = self.plan()
        tier(p, "L4")["gate"] = "auto"
        self.assertFails(p, "exceeds approved_budget_usd")

    def test_budget_too_small_fails(self):
        p = self.plan()
        p["approved_budget_usd"] = 10
        tier(p, "L4")["gate"] = "auto"
        self.assertFails(p, "exceeds approved_budget_usd")

    def test_cumulative_cost_is_tracked_across_tiers(self):
        p = self.plan()
        p["approved_budget_usd"] = 20
        tier(p, "L4")["gate"] = "auto"
        tier(p, "L2b")["gate"] = "auto"
        tier(p, "L2b")["estimated_cost_usd"] = 5
        self.assertFails(p, "cumulative estimate $23")

    def test_paid_tool_under_auto_needs_an_estimate(self):
        p = self.plan()
        tier(p, "L4")["gate"] = "auto"
        del tier(p, "L4")["estimated_cost_usd"]
        self.assertFails(p, "without 'estimated_cost_usd'")

    def test_cost_confirm_needs_an_estimate(self):
        p = self.plan()
        del tier(p, "L4")["estimated_cost_usd"]
        self.assertFails(p, "has no 'estimated_cost_usd'")

    def test_bad_cost_values(self):
        for bad in (True, -1, "18", float("inf")):
            with self.subTest(bad=bad):
                p = self.plan()
                tier(p, "L4")["estimated_cost_usd"] = bad
                self.assertFails(p, "must be a non-negative number")


class TestRiskGates(ValidatorCase):
    def test_l5_must_be_high_risk_confirm(self):
        for gate in ("auto", "cost_confirm"):
            with self.subTest(gate=gate):
                p = self.plan()
                tier(p, "L5")["gate"] = gate
                self.assertFails(p, "Safety violation: Tier L5")

    def test_risky_tool_names_cannot_hide_in_low_tiers(self):
        for name in ("stripe_payment", "deploy_prod", "merge_pull_request", "send_email"):
            with self.subTest(name=name):
                p = self.plan()
                tier(p, "L1")["tools"] = [name]
                self.assertFails(p, f"tool '{name}' signals an external or destructive action")

    def test_cost_confirm_is_not_enough_for_a_risky_tool(self):
        p = self.plan()
        t = tier(p, "L3")
        t["tools"], t["gate"], t["estimated_cost_usd"] = ["merge_pull_request"], "cost_confirm", 0
        self.assertFails(p, "use 'high_risk_confirm'")


class TestGraph(ValidatorCase):
    def test_l2_race_condition(self):
        p = self.plan()
        tier(p, "L2b")["depends_on"] = ["L1"]
        self.assertFails(p, "L2 Race Condition")

    def test_dependency_on_a_tier_missing_from_the_plan(self):
        p = self.plan()
        p["tiers"] = [t for t in p["tiers"] if t["tier_id"] != "L2a"]
        self.assertFails(p, "neither in this plan nor in satisfied_tiers")

    def test_dependency_on_same_or_downstream_tier(self):
        p = self.plan()
        tier(p, "L2a")["depends_on"] = ["L4"]
        self.assertFails(p, "cannot depend on same or downstream tier")

    def test_depends_on_must_be_a_list(self):
        p = self.plan()
        tier(p, "L2a")["depends_on"] = "L1"
        self.assertFails(p, "must be a list of tier ids")

    def test_duplicate_and_out_of_order_tiers(self):
        p = self.plan()
        p["tiers"].append(copy.deepcopy(tier(p, "L1")))
        self.assertFails(p, "appears more than once")
        p = self.plan()
        p["tiers"].reverse()
        self.assertFails(p, "listed out of order")

    def test_tier_before_entry_must_be_satisfied(self):
        p = self.plan()
        p["entry_tier"] = "L2b"
        self.assertFails(p, "precedes entry_tier")

    def test_rollback_target_must_be_upstream_and_present(self):
        p = self.plan()
        tier(p, "L2a")["rollback_target"] = "L4"
        self.assertFails(p, "must be an upstream tier")


class TestStructure(ValidatorCase):
    def test_missing_required_fields(self):
        p = self.plan()
        for key in ("role", "execution_mode", "expected_output"):
            del tier(p, "L1")[key]
        for key in ("role", "execution_mode", "expected_output"):
            self.assertFails(p, f"missing required field '{key}'")

    def test_malformed_input_reports_instead_of_crashing(self):
        self.assertFails([1, 2], "pipeline must be a JSON object")
        self.assertFails({}, "Missing required field")
        p = self.plan()
        p["tiers"].append("L1")
        self.assertFails(p, "must be an object")
        p = self.plan()
        tier(p, "L1")["tools"] = [1, 2]
        self.assertFails(p, "non-empty strings")

    def test_top_level_options(self):
        p = self.plan()
        p["max_retries"] = 9
        self.assertFails(p, "'max_retries' must be an integer between 0 and 5")
        p = self.plan()
        p["approved_budget_usd"] = -5
        self.assertFails(p, "'approved_budget_usd' must be a non-negative number")
        p = self.plan()
        p["macro_mode"] = "yes"
        self.assertFails(p, "'macro_mode' must be true or false")


class TestSubPipelines(ValidatorCase):
    def with_sub(self, parent_gate: str, parent_cost: object = None) -> dict:
        p = self.plan()
        p["macro_mode"] = True
        t = tier(p, "L3")
        t["gate"] = parent_gate
        if parent_cost is not None:
            t["estimated_cost_usd"] = parent_cost
        t["sub_pipeline"] = {"tiers": [{
            "tier_id": "L4", "role": "render", "tools": ["kling.generate_video"],
            "execution_mode": "sequential", "gate": "cost_confirm", "estimated_cost_usd": 18,
            "expected_output": "clip"}]}
        # the example's own L4 is now redundant; keep the total honest
        tier(p, "L4")["estimated_cost_usd"] = 0
        tier(p, "L4")["tools"] = ["ffmpeg-master"]
        tier(p, "L4")["gate"] = "auto"
        return p

    def test_weaker_parent_gate_is_rejected(self):
        self.assertFails(self.with_sub("auto"), "must bubble up")

    def test_parent_estimate_must_cover_children(self):
        self.assertFails(self.with_sub("cost_confirm", 5), "costs must bubble up")

    def test_properly_bubbled_sub_pipeline_passes(self):
        self.assertEqual(vp.validate_pipeline(self.with_sub("cost_confirm", 18)), [])


class TestSchemaAgreement(unittest.TestCase):
    """The hand-written validator and the JSON schema must not drift apart."""

    def test_required_fields(self):
        self.assertEqual(set(SCHEMA["required"]), set(vp.REQUIRED_TOP))
        self.assertEqual(set(SCHEMA["definitions"]["tier"]["required"]), set(vp.REQUIRED_TIER))

    def test_enums(self):
        defs = SCHEMA["definitions"]
        self.assertEqual(set(defs["tierId"]["enum"]), set(vp.TIER_ORDER))
        self.assertEqual(set(defs["gate"]["enum"]), set(vp.GATE_SEVERITY))
        self.assertEqual(set(defs["tier"]["properties"]["execution_mode"]["enum"]), vp.EXECUTION_MODES)

    def test_every_schema_property_is_known_to_the_validator_source(self):
        src = SCRIPT.read_text(encoding="utf-8")
        props = set(SCHEMA["properties"]) | set(SCHEMA["definitions"]["tier"]["properties"])
        for prop in props:
            self.assertTrue(prop in src, f"validator never mentions schema property '{prop}'")


class TestCli(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True)

    def test_exit_codes(self):
        ok = self.run_cli(str(REPO_ROOT / "assets" / "example-pipeline.json"))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        bad = self.run_cli(str(REPO_ROOT / "tests" / "fixtures" / "plan_paid_l4_auto.json"))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("FAILED", bad.stdout)
        self.assertEqual(self.run_cli("/nonexistent/plan.json").returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(self.run_cli(str(broken)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
