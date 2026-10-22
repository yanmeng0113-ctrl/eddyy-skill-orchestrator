#!/usr/bin/env python3
"""Tests for version 0.5.0 features:
- node-level rerun (failed node + downstream needs_rerun, others kept with artifacts)
- fallback chosen when tool unavailable
- budget overrun stops (exit 3 with 'stop and confirm')
- configured vs verified distinction and tool readiness
- old tier-format state file loads and migrates
- pipeline validation with node_id and fallback
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import checkpoint_state as cs  # noqa: E402
import scan_inventory as si  # noqa: E402
import validate_pipeline as vp  # noqa: E402


class CheckpointV05Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.file = self.dir / "state.json"

    def run_cmd(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cs.main(["--file", str(self.file), *args])
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def state(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def init(self, *extra: str) -> None:
        code, _, err = self.run_cmd("init", "--scope", "test-v05", *extra)
        self.assertEqual(code, 0, err)


class TestNodeLevelRerun(CheckpointV05Case):
    def test_rerun_invalidates_transitive_downstream_and_keeps_others(self):
        self.init()
        # DAG structure:
        # A -> B -> C
        # A -> D
        # E (independent)
        self.assertEqual(self.run_cmd("node-add", "--id", "A")[0], 0)
        self.assertEqual(self.run_cmd("node-add", "--id", "B", "--depends-on", "A")[0], 0)
        self.assertEqual(self.run_cmd("node-add", "--id", "C", "--depends-on", "B")[0], 0)
        self.assertEqual(self.run_cmd("node-add", "--id", "D", "--depends-on", "A")[0], 0)
        self.assertEqual(self.run_cmd("node-add", "--id", "E")[0], 0)

        # Complete all nodes with artifacts
        self.assertEqual(self.run_cmd("node-complete", "--id", "A", "--artifact", "/out/a.txt")[0], 0)
        self.assertEqual(self.run_cmd("node-complete", "--id", "B", "--artifact", "/out/b.txt")[0], 0)
        self.assertEqual(self.run_cmd("node-complete", "--id", "C", "--artifact", "/out/c.txt")[0], 0)
        self.assertEqual(self.run_cmd("node-complete", "--id", "D", "--artifact", "/out/d.txt")[0], 0)
        self.assertEqual(self.run_cmd("node-complete", "--id", "E", "--artifact", "/out/e.txt")[0], 0)

        s0 = self.state()
        self.assertEqual(s0["nodes"]["A"]["artifacts"], ["/out/a.txt"])
        self.assertEqual(s0["nodes"]["B"]["artifacts"], ["/out/b.txt"])
        self.assertEqual(s0["nodes"]["C"]["artifacts"], ["/out/c.txt"])
        self.assertEqual(s0["nodes"]["D"]["artifacts"], ["/out/d.txt"])
        self.assertEqual(s0["nodes"]["E"]["artifacts"], ["/out/e.txt"])

        # Mark B as failed
        code, _, _ = self.run_cmd("node-fail", "--id", "B")
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["nodes"]["B"]["status"], "failed")

        # Rerun B
        code, out, _ = self.run_cmd("rerun", "--id", "B", "--reason", "face consistency fix")
        self.assertEqual(code, 0)
        self.assertIn("needs_rerun", out)

        s1 = self.state()
        # B and its downstream node C must be needs_rerun with empty artifacts
        self.assertEqual(s1["nodes"]["B"]["status"], "needs_rerun")
        self.assertEqual(s1["nodes"]["B"]["artifacts"], [])
        self.assertEqual(s1["nodes"]["C"]["status"], "needs_rerun")
        self.assertEqual(s1["nodes"]["C"]["artifacts"], [])

        # Upstream A and independent nodes D, E must be untouched
        self.assertEqual(s1["nodes"]["A"]["status"], "completed")
        self.assertEqual(s1["nodes"]["A"]["artifacts"], ["/out/a.txt"])
        self.assertEqual(s1["nodes"]["D"]["status"], "completed")
        self.assertEqual(s1["nodes"]["D"]["artifacts"], ["/out/d.txt"])
        self.assertEqual(s1["nodes"]["E"]["status"], "completed")
        self.assertEqual(s1["nodes"]["E"]["artifacts"], ["/out/e.txt"])

        # Retries per node counter
        self.assertEqual(s1["retries"]["B"], 1)

    def test_rerun_respects_max_retries_and_exits_3(self):
        self.init("--max-retries", "2")
        self.run_cmd("node-add", "--id", "task1")
        self.run_cmd("node-complete", "--id", "task1", "--artifact", "out1.dat")

        # First rerun -> retry 1
        code1, _, _ = self.run_cmd("rerun", "--id", "task1")
        self.assertEqual(code1, 0)
        self.assertEqual(self.state()["retries"]["task1"], 1)

        # Second rerun -> retry 2
        code2, _, _ = self.run_cmd("rerun", "--id", "task1")
        self.assertEqual(code2, 0)
        self.assertEqual(self.state()["retries"]["task1"], 2)

        # Third rerun -> exceeds limit, exits 3
        code3, _, err = self.run_cmd("rerun", "--id", "task1")
        self.assertEqual(code3, cs.EXIT_RETRY)
        self.assertIn("retry limit reached", err)
        self.assertEqual(self.state()["status"], "escalated")


class TestFallbackSelection(CheckpointV05Case):
    def test_fallback_selection_order_and_exhaustion(self):
        self.init()
        self.run_cmd("node-add", "--id", "render", "--tool", "kling", "--fallback", "runway,luma")

        # Primary tool available
        code, out, _ = self.run_cmd("next-tool", "--id", "render", "--unavailable", "ffmpeg")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "kling")

        # Primary tool unavailable -> first fallback
        code, out, _ = self.run_cmd("next-tool", "--id", "render", "--unavailable", "kling")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "runway")

        # Primary and first fallback unavailable -> second fallback
        code, out, _ = self.run_cmd("next-tool", "--id", "render", "--unavailable", "kling,runway")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "luma")

        # All unavailable -> exit 1
        code, _, err = self.run_cmd("next-tool", "--id", "render", "--unavailable", "kling,runway,luma")
        self.assertEqual(code, cs.EXIT_ERROR)
        self.assertIn("no available tool", err)


class TestBudgetGuard(CheckpointV05Case):
    def test_budget_overrun_exits_3_with_stop_and_confirm(self):
        # Direct CLI invocation with overrun
        code, _, err = self.run_cmd("budget", "--approved", "100.0", "--spent", "80.0", "--next-cost", "30.0")
        self.assertEqual(code, cs.EXIT_RETRY)
        self.assertIn("stop and confirm", err)

    def test_budget_within_limit_passes(self):
        code, out, _ = self.run_cmd("budget", "--approved", "100.0", "--spent", "80.0", "--next-cost", "15.0")
        self.assertEqual(code, 0)
        self.assertIn("Budget ok", out)

    def test_budget_persists_in_state_and_triggers_on_next_cost(self):
        self.init()
        # Save approved and spent into state
        code, _, _ = self.run_cmd("budget", "--approved", "50.0", "--spent", "40.0", "--next-cost", "0.0")
        self.assertEqual(code, 0)
        s = self.state()
        self.assertEqual(s["approved_budget_usd"], 50.0)
        self.assertEqual(s["spent_usd"], 40.0)

        # Call with next cost that causes overrun using persisted state
        code, _, err = self.run_cmd("budget", "--next-cost", "15.0")
        self.assertEqual(code, cs.EXIT_RETRY)
        self.assertIn("stop and confirm", err)


class TestConfiguredVsVerified(unittest.TestCase):
    def test_status_configured_default_and_mark_verified(self):
        entry = {
            "name": "test-tool",
            "type": "skill",
            "description": "A testing utility",
            "status": "configured",
            "inputs": [],
            "outputs": [],
            "cost": "unknown",
            "permissions": [],
        }
        self.assertFalse(si.is_verified(entry))
        self.assertTrue(si.is_available(entry))

        # Mark verified (probe succeeded)
        si.mark_verified(entry, True)
        self.assertEqual(entry["status"], "verified")
        self.assertTrue(si.is_verified(entry))
        self.assertTrue(si.is_available(entry))

        # Mark verified false (probe failed)
        si.mark_verified(entry, False)
        self.assertEqual(entry["status"], "unavailable")
        self.assertFalse(si.is_verified(entry))
        self.assertFalse(si.is_available(entry))

        # Mark unavailable helper
        si.mark_verified(entry, True)
        si.mark_unavailable(entry)
        self.assertEqual(entry["status"], "unavailable")

    def test_scan_mcp_missing_command_marks_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text(json.dumps({
                "mcpServers": {
                    "broken_srv": {
                        "command": "nonexistent_executable_binary_12345",
                        "args": ["run"],
                    },
                    "valid_srv": {
                        "command": sys.executable,
                        "args": ["-c", "print(1)"],
                    },
                }
            }), encoding="utf-8")
            servers = si.scan_mcp_configs([cfg])
            by_name = {s["name"]: s for s in servers}

            self.assertEqual(by_name["broken_srv"]["status"], "unavailable")
            self.assertEqual(by_name["valid_srv"]["status"], "configured")

            # Check new fields present
            for s in servers:
                self.assertIn("inputs", s)
                self.assertIn("outputs", s)
                self.assertIn("cost", s)
                self.assertIn("permissions", s)
                self.assertIn("status", s)

    def test_scan_inventory_cli_verified_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: Planning tool\n---\nBody\n",
                encoding="utf-8",
            )
            out_json = Path(tmp) / "out.json"
            orig_roots = si.default_skill_roots

            try:
                si.default_skill_roots = lambda home, cwd: [Path(tmp)]
                # Run CLI with --verified my-skill
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    code = si.main([str(out_json), "--verified", "my-skill"])
                self.assertEqual(code, 0)
                data = json.loads(out_json.read_text(encoding="utf-8"))
                found = next(item for item in data["inventory"] if item["name"] == "my-skill")
                self.assertEqual(found["status"], "verified")
            finally:
                si.default_skill_roots = orig_roots

    def test_cost_and_permissions_detection(self):
        self.assertEqual(si.detect_cost("Free local script", declared="free"), "free")
        self.assertEqual(si.detect_cost("Pay per second Kling video generation"), "paid")
        self.assertEqual(si.detect_cost("Local helper without cost keywords"), "unknown")

        perms_destructive = si.detect_permissions("Drop database prod_db and wipe disk")
        self.assertIn("destructive", perms_destructive)

        perms_external = si.detect_permissions("Publish video to youtube and deploy app")
        self.assertIn("external", perms_external)

        perms_paid = si.detect_permissions("Kling video generation pay per second")
        self.assertIn("paid", perms_paid)


class TestOldTierStateMigration(CheckpointV05Case):
    def test_legacy_v1_v2_completed_tiers_migrate_to_nodes(self):
        legacy = {
            "schema_version": 2,
            "scope": "legacy-pipeline",
            "entry_tier": "L1",
            "current_tier": "L2b",
            "status": "in_progress",
            "locked_anchors": {"character_seed": "42"},
            "completed_tiers": {
                "L1": {"output": "script.md", "timestamp": "2026-10-20T10:00:00Z"},
                "L2a": {"output": "character_sheet.png", "timestamp": "2026-10-20T10:05:00Z"},
            },
            "retries": {},
            "max_retries": 2,
            "confirmations": [],
        }
        self.file.write_text(json.dumps(legacy), encoding="utf-8")

        # Load state
        s = cs.load_state(self.file)
        self.assertIn("nodes", s)
        self.assertIn("L1", s["nodes"])
        self.assertIn("L2a", s["nodes"])

        # Node L1 checks
        node_l1 = s["nodes"]["L1"]
        self.assertEqual(node_l1["status"], "completed")
        self.assertEqual(node_l1["artifacts"], ["script.md"])
        self.assertEqual(node_l1["depends_on"], [])

        # Node L2a checks (depends on previous tier L1 by order)
        node_l2a = s["nodes"]["L2a"]
        self.assertEqual(node_l2a["status"], "completed")
        self.assertEqual(node_l2a["artifacts"], ["character_sheet.png"])
        self.assertEqual(node_l2a["depends_on"], ["L1"])

        # Verify old command complete-tier continues to work and updates nodes
        code, _, err = self.run_cmd("complete-tier", "--tier", "L2b", "--output", "keyframes.zip", "--next-tier", "L3")
        self.assertEqual(code, 0, err)

        saved = self.state()
        self.assertEqual(saved["schema_version"], 3)
        self.assertIn("L2b", saved["completed_tiers"])
        self.assertIn("L2b", saved["nodes"])
        self.assertEqual(saved["nodes"]["L2b"]["status"], "completed")
        self.assertEqual(saved["nodes"]["L2b"]["artifacts"], ["keyframes.zip"])
        self.assertEqual(saved["nodes"]["L2b"]["depends_on"], ["L2a"])


class TestPipelineSchemaV05(unittest.TestCase):
    def test_pipeline_with_node_id_and_fallback_and_node_dependencies(self):
        plan = {
            "scope": "Video pipeline with node IDs and fallbacks",
            "entry_tier": "L1",
            "tiers": [
                {
                    "tier_id": "L1",
                    "node_id": "plan_node",
                    "role": "spec",
                    "tools": ["writer"],
                    "fallback": ["backup_writer"],
                    "execution_mode": "sequential",
                    "gate": "auto",
                    "expected_output": "script",
                },
                {
                    "tier_id": "L2a",
                    "node_id": "anchor_node",
                    "role": "sheet",
                    "tools": ["artist"],
                    "depends_on": ["plan_node"],
                    "execution_mode": "sequential",
                    "gate": "auto",
                    "expected_output": "character sheet",
                },
            ],
        }
        errors = vp.validate_pipeline(plan)
        self.assertEqual(errors, [])

    def test_pipeline_rejects_downstream_node_dependency(self):
        plan = {
            "scope": "Invalid downstream dependency",
            "entry_tier": "L1",
            "tiers": [
                {
                    "tier_id": "L1",
                    "node_id": "n1",
                    "role": "spec",
                    "tools": ["writer"],
                    "depends_on": ["n2"],  # n2 is downstream in L2a
                    "execution_mode": "sequential",
                    "gate": "auto",
                    "expected_output": "script",
                },
                {
                    "tier_id": "L2a",
                    "node_id": "n2",
                    "role": "sheet",
                    "tools": ["artist"],
                    "execution_mode": "sequential",
                    "gate": "auto",
                    "expected_output": "sheet",
                },
            ],
        }
        errors = vp.validate_pipeline(plan)
        self.assertTrue(any("cannot depend on same or downstream" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
