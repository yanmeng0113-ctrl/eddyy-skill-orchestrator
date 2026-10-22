#!/usr/bin/env python3
"""Regression tests for scripts/scan_inventory.py."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scan_inventory import (  # noqa: E402
    classify_text, classify_tool_name, normalize, parse_frontmatter, scan_mcp_configs, scan_skills,
)


class TestBlindSamples(unittest.TestCase):
    """Real-world skills and MCPs the original suite covered; must keep passing."""

    def test_planning_interview_is_l1_auto(self):
        r = classify_text("grill-me A relentless interview to sharpen a plan or design.")
        self.assertEqual(r["candidate_tiers"], ["L1"])
        self.assertEqual(r["default_gate"], "auto")

    def test_ambient_skills(self):
        r = classify_text("caveman Terse caveman voice: answer first, fluff gone, talk like caveman, less tokens.")
        self.assertIn("L0-Ambient", r["candidate_tiers"])
        r = classify_text("apple-design Apple approach to interface design and design foundations when building UI.")
        self.assertEqual(r["candidate_tiers"], ["L0-Ambient"])

    def test_canvas_composite_has_no_false_delete_trigger(self):
        r = classify_text("figma-use create/edit/delete nodes, set up variables or tokens, "
                          "build components and variants, modify auto-layout")
        self.assertEqual(r["candidate_tiers"], ["L2a", "L2b", "L3"])
        self.assertEqual(r["default_gate"], "auto")

    def test_studio_mcp_is_multi_tier_and_cost_gated(self):
        r = classify_text("mcp-kling Kling AI video generation! Generate videos, create images with KOLORS, "
                          "add lip-sync, check account_balance.")
        self.assertEqual(r["candidate_tiers"], ["L2b", "L3", "L4"])
        self.assertTrue(r["is_multi_tier_or_composite"])
        self.assertEqual(r["default_gate"], "cost_confirm")

    def test_chinese_tools(self):
        r = classify_text("角色三视图生成 根据剧本生成主角三视图并锁定种子")
        self.assertIn("L2a", r["candidate_tiers"])
        r = classify_text("小红书自动发布器 将视频和图文一键发布到小红书")
        self.assertEqual(r["candidate_tiers"], ["L5"])
        self.assertEqual(r["default_gate"], "high_risk_confirm")


class TestFailClosed(unittest.TestCase):
    """The blacklist used to miss all of these and answer L2b + auto."""

    RISKY = [
        "git push --force origin main", "rm -rf /var/data", "drop database prod_db",
        "transfer funds to account", "purchase a subscription", "buy credits",
        "send email to customers", "merge pull request #42", "stripe_payment", "deploy_prod",
        "git reset --hard HEAD~3", "kubectl apply -f prod.yaml", "delete_branch", "revoke access for all users",
        "terraform destroy cluster",
    ]

    def test_risky_actions_need_high_risk_confirm(self):
        for text in self.RISKY:
            with self.subTest(text=text):
                r = classify_text(text)
                self.assertEqual(r["default_gate"], "high_risk_confirm", r)
                self.assertTrue(r["external_mutation_risk"])
                self.assertIn("L5", r["candidate_tiers"])

    def test_paid_service_needs_cost_confirm(self):
        for text in ["paid GPT API call", "Sora text-to-video generation", "ElevenLabs voiceover"]:
            with self.subTest(text=text):
                self.assertEqual(classify_text(text)["default_gate"], "cost_confirm")

    def test_unknown_tool_is_not_silently_auto(self):
        for text in ["media_cloud_runner run jobs in the cloud", "test-runner run unit tests"]:
            with self.subTest(text=text):
                r = classify_text(text)
                self.assertEqual(r["default_gate"], "probe_required")
                self.assertTrue(r["needs_probe"])
                self.assertEqual(r["candidate_tiers"], [])

    def test_underscore_identifiers_are_normalised(self):
        self.assertEqual(normalize("stripe_payment-v2.create/charge"), "stripe payment v2 create charge")
        self.assertEqual(normalize("ElevenLabsTTS", split_camel=True), "eleven labs tts")


class TestFalsePositives(unittest.TestCase):
    NOT_HIGH_RISK = [
        "发布会策划方案调研", "支付宝账单查询（只读）", "线上线下联动活动策划", "工作部署方案策划",
        "客户购买意向调研", "公众号文章写作 根据大纲撰写文案", "新品发布会物料设计",
        "video production pipeline script storyboard", "truncate long text output",
        "git checkout feature branch", "YouTube transcript reader (read-only)",
        "stripe read-only reporting of last month charges",
    ]

    def test_ordinary_text_is_not_high_risk(self):
        for text in self.NOT_HIGH_RISK:
            with self.subTest(text=text):
                r = classify_text(text)
                self.assertNotEqual(r["default_gate"], "high_risk_confirm", r)

    def test_read_only_never_downgrades_destructive_actions(self):
        r = classify_text("read-only helper that can also drop database tables")
        self.assertEqual(r["default_gate"], "high_risk_confirm")

    def test_chinese_publishing_still_flagged_when_real(self):
        for text in ["公众号文章写作 根据大纲撰写文案并发布到公众号", "支付宝支付插件", "群发短信工具"]:
            with self.subTest(text=text):
                self.assertEqual(classify_text(text)["default_gate"], "high_risk_confirm")


class TestToolNames(unittest.TestCase):
    def test_risky_names(self):
        for name in ["stripe_payment", "deploy_prod", "merge_pull_request", "youtube_upload_video", "send_email"]:
            with self.subTest(name=name):
                self.assertTrue(classify_tool_name(name)["risk"], name)

    def test_paid_names(self):
        for name in ["kling.generate_video", "ElevenLabsTTS", "runway_gen3"]:
            with self.subTest(name=name):
                self.assertTrue(classify_tool_name(name)["cost"], name)

    def test_harmless_names(self):
        for name in ["github", "create_or_update_file", "get_credit_balance", "char-3view", "list_tables"]:
            with self.subTest(name=name):
                sig = classify_tool_name(name)
                self.assertEqual((sig["risk"], sig["cost"]), ([], []), name)


class TestFrontmatter(unittest.TestCase):
    def test_block_and_quoted_scalars(self):
        self.assertEqual(
            parse_frontmatter("---\nname: foo\ndescription: >\n  Render and publish videos\n  with ffmpeg.\n---\nbody")["description"],
            "Render and publish videos with ffmpeg.")
        self.assertEqual(parse_frontmatter("---\nname: b\ndescription: |\n  line1\n  line2\n---\n")["description"],
                         "line1\nline2")
        self.assertEqual(parse_frontmatter("---\r\nname: c\r\ndescription: 'quoted: value'\r\n---\r\n")["description"],
                         "quoted: value")
        self.assertEqual(parse_frontmatter("---\nname: d\ndescription: plain\n  continued\n---\n")["description"],
                         "plain continued")

    def test_no_frontmatter(self):
        self.assertIsNone(parse_frontmatter("# just markdown"))

    def test_scan_skills_reads_multiline_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "foo"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: foo\ndescription: >\n  Publish videos to YouTube\n  with ffmpeg.\n---\nbody\n", encoding="utf-8")
            found = scan_skills([Path(tmp)])
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0]["description"], ">")
        self.assertEqual(found[0]["default_gate"], "high_risk_confirm")


class TestMcpConfigs(unittest.TestCase):
    def _scan(self, data: dict):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text(json.dumps(data), encoding="utf-8")
            return scan_mcp_configs([cfg])

    def test_env_and_headers_are_ignored_and_never_emitted(self):
        out = self._scan({"mcpServers": {"foo": {
            "command": "npx", "args": ["-y", "foo-mcp"],
            "env": {"API_KEY": "sk-SECRET-123", "MODE": "deploy production"},
            "headers": {"Authorization": "Bearer TOPSECRET"}}}})
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertFalse(out[0]["external_mutation_risk"])
        self.assertTrue(out[0]["needs_probe"])

    def test_name_command_and_url_host_are_used(self):
        out = self._scan({"mcpServers": {
            "stripe": {"url": "https://mcp.stripe.com/?token=abc"},
            "plain": {"command": "node", "args": ["server.js"]}}})
        by = {s["name"]: s for s in out}
        self.assertEqual(by["stripe"]["default_gate"], "high_risk_confirm")
        self.assertNotIn("abc", json.dumps(out))

    def test_per_project_servers_in_claude_json(self):
        out = self._scan({"projects": {"/work/app": {"mcpServers": {"kling-mcp": {"command": "kling-mcp"}}}}})
        self.assertEqual([s["name"] for s in out], ["kling-mcp"])
        self.assertEqual(out[0]["default_gate"], "cost_confirm")


if __name__ == "__main__":
    unittest.main()
