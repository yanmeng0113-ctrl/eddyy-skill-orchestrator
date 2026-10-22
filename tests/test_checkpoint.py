#!/usr/bin/env python3
"""Tests for scripts/checkpoint_state.py."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import checkpoint_state as cs  # noqa: E402


class CheckpointCase(unittest.TestCase):
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
            except SystemExit as exc:  # argparse errors
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def state(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def init(self, *extra: str) -> None:
        code, _, err = self.run_cmd("init", "--scope", "demo", *extra)
        self.assertEqual(code, 0, err)


class TestBasics(CheckpointCase):
    def test_init_lock_complete_show(self):
        self.init()
        self.assertEqual(self.run_cmd("lock", "--key", "seed", "--value", "42")[0], 0)
        self.assertEqual(self.run_cmd("complete-tier", "--tier", "L1", "--output", "script.md", "--next-tier", "L2a")[0], 0)
        s = self.state()
        self.assertEqual(s["locked_anchors"], {"seed": "42"})
        self.assertIn("L1", s["completed_tiers"])
        self.assertEqual(s["current_tier"], "L2a")
        code, out, _ = self.run_cmd("show", "--brief")
        self.assertEqual(code, 0)
        self.assertIn("seed=42", out)
        self.assertIn("continue at L2a", out)

    def test_no_temp_files_left_behind(self):
        self.init()
        self.run_cmd("lock", "--key", "a", "--value", "1")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["state.json"])

    def test_tier_names_are_validated(self):
        self.init()
        for bad in ("L9", "l1", "L2", "", "L3.x"):
            with self.subTest(tier=bad):
                self.assertEqual(self.run_cmd("complete-tier", "--tier", bad, "--output", "x")[0], cs.EXIT_ERROR)
        self.assertEqual(self.run_cmd("complete-tier", "--tier", "L3.2a", "--output", "nested ok")[0], 0)

    def test_commands_need_an_initialised_file(self):
        code, _, err = self.run_cmd("lock", "--key", "a", "--value", "1")
        self.assertEqual(code, cs.EXIT_ERROR)
        self.assertIn("run `init` first", err)

    def test_legacy_v1_state_is_upgraded_on_load(self):
        self.file.write_text(json.dumps({"scope": "old", "entry_tier": "L1", "current_tier": "L2a",
                                         "status": "in_progress", "locked_anchors": {"seed": "7"},
                                         "completed_tiers": {}}), encoding="utf-8")
        self.assertEqual(self.run_cmd("lock", "--key", "style", "--value", "noir")[0], 0)
        s = self.state()
        self.assertEqual(s["locked_anchors"], {"seed": "7", "style": "noir"})
        self.assertEqual(s["max_retries"], 2)
        self.assertEqual(s["schema_version"], cs.SCHEMA_VERSION)


class TestAnchorsAndInit(CheckpointCase):
    def test_locked_anchor_is_not_silently_overwritten(self):
        self.init()
        self.run_cmd("lock", "--key", "seed", "--value", "42")
        self.assertEqual(self.run_cmd("lock", "--key", "seed", "--value", "43")[0], cs.EXIT_ERROR)
        self.assertEqual(self.state()["locked_anchors"]["seed"], "42")
        self.assertEqual(self.run_cmd("lock", "--key", "seed", "--value", "43", "--force")[0], 0)
        self.assertEqual(self.state()["locked_anchors"]["seed"], "43")
        self.assertEqual(self.run_cmd("lock", "--key", "seed", "--value", "43")[0], 0)  # same value is fine

    def test_init_refuses_to_replace_a_running_pipeline(self):
        self.init()
        self.run_cmd("lock", "--key", "seed", "--value", "42")
        code, _, err = self.run_cmd("init", "--scope", "other")
        self.assertEqual(code, cs.EXIT_ERROR)
        self.assertIn("still in progress", err)
        self.assertEqual(self.state()["locked_anchors"], {"seed": "42"})

    def test_init_force_backs_up_the_old_state(self):
        self.init()
        self.run_cmd("lock", "--key", "seed", "--value", "42")
        self.assertEqual(self.run_cmd("init", "--scope", "fresh", "--force")[0], 0)
        backups = list(self.dir.glob("state.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["locked_anchors"], {"seed": "42"})
        self.assertEqual(self.state()["scope"], "fresh")

    def test_a_completed_pipeline_can_be_replaced(self):
        self.init()
        self.run_cmd("complete-tier", "--tier", "L5", "--output", "done", "--status", "completed")
        self.assertEqual(self.run_cmd("init", "--scope", "next")[0], 0)


class TestCorruption(CheckpointCase):
    def test_corrupt_state_is_never_reset(self):
        self.init()
        self.run_cmd("lock", "--key", "seed", "--value", "42")
        truncated = '{"scope": "demo", "locked_anchors": {"seed": "42"'
        self.file.write_text(truncated, encoding="utf-8")
        for cmd in (("lock", "--key", "style", "--value", "noir"), ("show",), ("complete-tier", "--tier", "L1", "--output", "x")):
            with self.subTest(cmd=cmd[0]):
                code, _, err = self.run_cmd(*cmd)
                self.assertEqual(code, cs.EXIT_CORRUPT)
                self.assertIn("unreadable", err)
                self.assertEqual(self.file.read_text(encoding="utf-8"), truncated)
        backups = list(self.dir.glob("state.json.corrupt-*"))
        self.assertEqual(len(backups), 1, "identical corrupt content must not create repeated backups")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), truncated)

    def test_wrong_structure_counts_as_corrupt(self):
        self.file.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self.run_cmd("show")[0], cs.EXIT_CORRUPT)

    def test_init_on_a_corrupt_file_needs_force(self):
        self.file.write_text("{oops", encoding="utf-8")
        self.assertEqual(self.run_cmd("init", "--scope", "x")[0], cs.EXIT_ERROR)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "{oops")
        self.assertEqual(self.run_cmd("init", "--scope", "x", "--force")[0], 0)
        self.assertEqual(self.state()["scope"], "x")


class TestGatesAndRollback(CheckpointCase):
    def test_confirm_flow(self):
        self.init()
        self.assertEqual(self.run_cmd("confirm", "--tier", "L4")[0], cs.EXIT_ERROR)  # nothing pending
        self.run_cmd("complete-tier", "--tier", "L3", "--output", "timeline", "--next-tier", "L4",
                     "--status", "awaiting_confirmation")
        out = self.run_cmd("show", "--brief")[1]
        self.assertIn("go/no-go before L4", out)
        self.assertEqual(self.run_cmd("confirm", "--tier", "L3")[0], cs.EXIT_ERROR)  # wrong tier
        self.assertEqual(self.run_cmd("confirm", "--tier", "L4", "--note", "USD 18 approved")[0], 0)
        s = self.state()
        self.assertEqual(s["status"], "in_progress")
        self.assertEqual(s["confirmations"][0]["note"], "USD 18 approved")

    def test_rollback_counts_retries_and_stops_at_the_limit(self):
        self.init()
        self.run_cmd("complete-tier", "--tier", "L1", "--output", "script", "--next-tier", "L2a")
        self.run_cmd("complete-tier", "--tier", "L2a", "--output", "sheet", "--next-tier", "L2b")
        self.run_cmd("complete-tier", "--tier", "L2b", "--output", "frames", "--next-tier", "L3")
        self.assertEqual(self.run_cmd("rollback", "--to", "L2b", "--reason", "face drift")[0], 0)
        s = self.state()
        self.assertEqual(s["current_tier"], "L2b")
        self.assertEqual(s["status"], "rolled_back")
        self.assertEqual(sorted(s["completed_tiers"]), ["L1", "L2a"], "L1 and L2a must survive a rollback to L2b")
        self.assertEqual(self.run_cmd("rollback", "--to", "L2b")[0], 0)
        code, _, err = self.run_cmd("rollback", "--to", "L2b")
        self.assertEqual(code, cs.EXIT_RETRY)
        self.assertIn("escalate", err)
        self.assertEqual(self.state()["status"], "escalated")
        self.assertIn("do not continue", self.run_cmd("show", "--brief")[1])

    def test_retry_limit_is_configurable_and_per_tier(self):
        self.init("--max-retries", "1")
        self.assertEqual(self.run_cmd("rollback", "--to", "L2b")[0], 0)
        self.assertEqual(self.run_cmd("rollback", "--to", "L2b")[0], cs.EXIT_RETRY)
        self.init("--force", "--max-retries", "1")
        self.assertEqual(self.run_cmd("rollback", "--to", "L2b")[0], 0)
        self.assertEqual(self.run_cmd("rollback", "--to", "L3")[0], 0, "retries are counted per tier")

    def test_rollback_to_an_anchor_tier_warns_about_stale_anchors(self):
        self.init()
        self.run_cmd("lock", "--key", "seed", "--value", "42")
        code, out, _ = self.run_cmd("rollback", "--to", "L2a")
        self.assertEqual(code, 0)
        self.assertIn("lock --force", out)


if __name__ == "__main__":
    unittest.main()
