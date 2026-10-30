from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(os.environ.get("CHECKPOINT_REPO", Path(__file__).resolve().parents[1]))
SCRIPT = REPO / "scripts" / "checkpoint_state.py"
TEMP_ROOT = Path(os.environ.get("RELIABILITY_TEMP_ROOT", os.environ.get("TEMP", tempfile.gettempdir())))
CASE_LOG = Path(os.environ.get("NEW_RELIABILITY_CLI_LOG", TEMP_ROOT / "new-reliability-cli.jsonl"))


class CheckpointCliCase(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="new-case-", dir=TEMP_ROOT)
        self.work = Path(self.temp.name)
        self.state_path = self.work / "checkpoint.json"
        self.addCleanup(self.temp.cleanup)

    def cli(self, *args: str, state_path: Path | None = None) -> subprocess.CompletedProcess[str]:
        path = state_path or self.state_path
        command = [sys.executable, str(SCRIPT), "--file", str(path), *map(str, args)]
        result = subprocess.run(
            command,
            cwd=self.work,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        record = {
            "test": self.id(),
            "test_module_file": str(Path(__file__).resolve()),
            "module_file": str(SCRIPT.resolve()),
            "command": command,
            "cwd": str(self.work),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        CASE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CASE_LOG.open("a", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    def ok(self, *args: str, state_path: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = self.cli(*args, state_path=state_path)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def data(self, path: Path | None = None) -> dict:
        return json.loads((path or self.state_path).read_text(encoding="utf-8"))


class NestedTierRecoveryTests(CheckpointCliCase):
    def test_nested_numeric_rollback_order_and_artifact_refs(self) -> None:
        artifacts: dict[str, Path] = {}
        for tier in ("L3.1", "L3.2", "L3.2a", "L3.2b", "L3.10", "L4"):
            artifact = self.work / f"{tier}.artifact"
            artifact.write_text(f"real output {tier}\n", encoding="utf-8", newline="")
            artifacts[tier] = artifact
        self.ok("init", "--scope", "nested numeric ordering", "--entry", "L3")
        for tier, artifact in artifacts.items():
            self.ok("complete-tier", "--tier", tier, "--output", str(artifact))

        self.ok("rollback", "--to", "L3.2a", "--reason", "nested ordering")
        state = self.data()
        self.assertEqual(set(state["completed_tiers"]), {"L3.1", "L3.2"})
        self.assertEqual(state["current_tier"], "L3.2a")
        for tier in ("L3.2a", "L3.2b", "L3.10", "L4"):
            with self.subTest(tier=tier):
                self.assertNotIn(tier, state["completed_tiers"])
                self.assertIn(state["nodes"][tier]["status"], ("needs_rerun", "pending"))
                self.assertEqual(state["nodes"][tier]["artifacts"], [])
        self.assertEqual(state["nodes"]["L3.1"]["status"], "completed")
        self.assertEqual(state["nodes"]["L3.2"]["status"], "completed")
        for artifact in artifacts.values():
            self.assertTrue(artifact.is_file(), f"state rollback deleted a real artifact: {artifact}")

        later_path = self.work / "rollback-later.json"
        self.ok("init", "--scope", "numeric boundary", "--entry", "L3", state_path=later_path)
        for tier in ("L3.2", "L3.10"):
            self.ok("complete-tier", "--tier", tier, "--output", tier, state_path=later_path)
        self.ok("rollback", "--to", "L3.10", state_path=later_path)
        later = self.data(later_path)
        self.assertIn("L3.2", later["completed_tiers"])
        self.assertNotIn("L3.10", later["completed_tiers"])

    def test_stage_rollback_save_reload_and_recompletion_position(self) -> None:
        self.ok("init", "--scope", "stage recovery", "--entry", "L1")
        for tier, output, next_tier in (
            ("L1", "plan.md", "L2a"),
            ("L2a", "anchor.png", "L2b"),
            ("L2b", "render.mp4", "L3"),
        ):
            self.ok("complete-tier", "--tier", tier, "--output", output, "--next-tier", next_tier)

        self.ok("rollback", "--to", "L2a", "--reason", "rebuild stage and later")
        self.ok("show", "--brief")
        self.ok("node-add", "--id", "save-after-stage-rollback")
        after_reload = self.data()
        self.assertEqual(set(after_reload["completed_tiers"]), {"L1"})
        for tier in ("L2a", "L2b"):
            self.assertEqual(after_reload["nodes"][tier]["status"], "needs_rerun")
            self.assertEqual(after_reload["nodes"][tier]["artifacts"], [])

        self.ok("node-complete", "--id", "L2a", "--artifact", "anchor-restored.png")
        after_first = self.data()
        self.assertEqual(after_first["completed_tiers"]["L2a"]["output"], "anchor-restored.png")
        self.assertEqual(after_first["current_tier"], "L2b")
        first_brief = self.ok("show", "--brief").stdout
        self.assertIn("NEXT: continue at L2b.", first_brief)

        self.ok("node-complete", "--id", "L2b", "--artifact", "render-restored.mp4")
        after_second = self.data()
        self.assertEqual(after_second["completed_tiers"]["L2b"]["output"], "render-restored.mp4")
        self.assertEqual(after_second["current_tier"], "L3")
        final_brief = self.ok("show", "--brief").stdout
        self.assertIn("NEXT: continue at L3.", final_brief)

    def test_arbitrary_tier_shaped_node_is_not_guessed_as_a_stage(self) -> None:
        self.ok("init", "--scope", "arbitrary node id", "--entry", "L1")
        self.ok("node-add", "--id", "L3.2a")
        self.ok("node-complete", "--id", "L3.2a", "--artifact", "ordinary-node.dat")
        state = self.data()
        self.assertNotIn("L3.2a", state["completed_tiers"])
        self.assertNotIn("tier_id", state["nodes"]["L3.2a"])


class GenericRerunRecoveryTests(CheckpointCliCase):
    def test_node_rerun_recovery_follows_explicit_dependency_chain(self) -> None:
        self.ok("init", "--scope", "generic node recovery", "--entry", "L1")
        for node, deps in (("upstream", ""), ("target", "upstream"), ("dependent", "target"), ("parallel", "upstream")):
            self.ok("node-add", "--id", node, "--depends-on", deps)
            self.ok("node-complete", "--id", node, "--artifact", f"{node}.dat")
        self.ok("rerun", "--id", "target", "--reason", "recover dependency chain")
        first = self.ok("show", "--brief").stdout
        self.assertIn("Position: target (rolled_back)", first)
        self.assertIn("NEXT: re-run node target.", first)
        self.ok("node-complete", "--id", "target", "--artifact", "target-restored.dat")
        second = self.ok("show", "--brief").stdout
        self.assertIn("Position: dependent (in_progress)", second)
        self.assertIn("NEXT: re-run node dependent.", second)
        state = self.data()
        self.assertEqual(state["nodes"]["upstream"]["status"], "completed")
        self.assertEqual(state["nodes"]["parallel"]["status"], "completed")
        self.assertEqual(state["nodes"]["dependent"]["status"], "needs_rerun")
        self.ok("node-complete", "--id", "dependent", "--artifact", "dependent-restored.dat")
        third = self.ok("show", "--brief").stdout
        self.assertIn("Position: L1 (in_progress)", third)
        final = self.data()
        self.assertEqual(final["nodes"]["target"]["artifacts"], ["target-restored.dat"])
        self.assertEqual(final["nodes"]["dependent"]["artifacts"], ["dependent-restored.dat"])


class BudgetFieldValidationTests(CheckpointCliCase):
    def test_budget_rejects_json_boolean_null_and_numeric_nonfinite_fields(self) -> None:
        invalid = (True, None, -1, float("nan"), float("inf"), float("-inf"))
        for field in ("approved_budget_usd", "spent_usd", "budget.approved", "budget.spent"):
            for index, value in enumerate(invalid):
                with self.subTest(field=field, value=repr(value)):
                    state_path = self.work / f"{field.replace('.', '-')}-{index}.json"
                    data = {"scope": "budget numeric field", "locked_anchors": {}, "completed_tiers": {}}
                    if field.startswith("budget."):
                        data["budget"] = {"approved": 100, "spent": 0}
                        data["budget"][field.split(".", 1)[1]] = value
                    else:
                        data["approved_budget_usd"] = 100
                        data["spent_usd"] = 0
                        data[field] = value
                    state_path.write_text(json.dumps(data, allow_nan=True), encoding="utf-8", newline="")
                    before = state_path.read_bytes()
                    result = self.cli("budget", "--next-cost", "0", state_path=state_path)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn("Budget ok", result.stdout)
                    self.assertEqual(state_path.read_bytes(), before)

    def test_missing_one_of_current_or_legacy_budget_values_is_not_assumed_zero(self) -> None:
        for data in (
            {"scope": "missing current spend", "approved_budget_usd": 100},
            {"scope": "missing current approval", "spent_usd": 0},
            {"scope": "missing legacy spend", "budget": {"approved": 100}},
            {"scope": "missing legacy approval", "budget": {"spent": 0}},
        ):
            state_path = self.work / f"missing-{len(list(self.work.glob('missing-*.json')))}.json"
            state_path.write_text(json.dumps(data), encoding="utf-8", newline="")
            result = self.cli("budget", state_path=state_path)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Budget ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
