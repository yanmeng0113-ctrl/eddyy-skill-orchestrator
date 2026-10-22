#!/usr/bin/env python3
"""Tests for OpenAI Codex support in scripts/scan_inventory.py."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib
except ImportError:
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scan_inventory  # noqa: E402
from scan_inventory import (  # noqa: E402
    default_mcp_configs,
    default_skill_roots,
    scan_mcp_configs,
    scan_skills,
)


class TestCodexSkillRoots(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.home = self.tmp_dir / "home"
        self.cwd = self.tmp_dir / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()

    def test_default_skill_roots_without_codex_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            roots = default_skill_roots(self.home, self.cwd)
            self.assertIn(self.home / ".codex" / "skills", roots)

    def test_scan_skills_in_default_codex_dir(self):
        skill_dir = self.home / ".codex" / "skills" / "codex-helper"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: codex-helper\ndescription: A planning and architecture helper.\n---\nBody\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            roots = default_skill_roots(self.home, self.cwd)
            found = scan_skills(roots)
            by_name = {s["name"]: s for s in found}
            self.assertIn("codex-helper", by_name)
            self.assertEqual(by_name["codex-helper"]["path"], str(skill_dir / "SKILL.md"))
            self.assertIn("L1", by_name["codex-helper"]["candidate_tiers"])

    def test_default_skill_roots_with_codex_home(self):
        custom_codex = self.tmp_dir / "custom_codex"
        custom_codex.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_codex)}):
            roots = default_skill_roots(self.home, self.cwd)
            self.assertIn(custom_codex / "skills", roots)
            self.assertNotIn(self.home / ".codex" / "skills", roots)

    def test_empty_codex_home_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            roots = default_skill_roots(self.home, self.cwd)
            self.assertIn(self.home / ".codex" / "skills", roots)

    def test_scan_skills_in_codex_home(self):
        custom_codex = self.tmp_dir / "custom_codex"
        skill_dir = custom_codex / "skills" / "custom-codex-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: custom-codex-skill\ndescription: Custom skill in CODEX_HOME.\n---\nBody\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_codex)}):
            roots = default_skill_roots(self.home, self.cwd)
            found = scan_skills(roots)
            by_name = {s["name"]: s for s in found}
            self.assertIn("custom-codex-skill", by_name)
            self.assertEqual(by_name["custom-codex-skill"]["path"], str(skill_dir / "SKILL.md"))


class TestCodexRepoAndAdminRoots(unittest.TestCase):
    def test_parent_agents_skills_up_to_git_root(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "repo"
            sub = root / "svc" / "mod"
            sub.mkdir(parents=True)
            (root / ".git").mkdir()
            roots = default_skill_roots(Path(t) / "home", sub)
            self.assertIn(root / ".agents" / "skills", roots)
            self.assertIn(root / "svc" / ".agents" / "skills", roots)
            self.assertNotIn(Path(t) / ".agents" / "skills", roots)

    def test_no_parent_dirs_outside_git(self):
        with tempfile.TemporaryDirectory() as t:
            sub = Path(t) / "a" / "b"
            sub.mkdir(parents=True)
            roots = default_skill_roots(Path(t) / "home", sub)
            self.assertNotIn(Path(t) / "a" / ".agents" / "skills", roots)

    def test_admin_root_listed(self):
        roots = default_skill_roots(Path("/nonexistent-home"), Path("/nonexistent-cwd"))
        self.assertIn(Path("/etc/codex/skills"), roots)


class TestCodexMcpConfigs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.home = self.tmp_dir / "home"
        self.cwd = self.tmp_dir / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()

    def test_default_mcp_configs_paths(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            cfgs = default_mcp_configs(self.home, self.cwd)
            self.assertIn(self.cwd / ".codex" / "config.toml", cfgs)
            self.assertIn(self.home / ".codex" / "config.toml", cfgs)

    def test_default_mcp_configs_with_codex_home(self):
        custom_codex = self.tmp_dir / "custom_codex"
        custom_codex.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_codex)}):
            cfgs = default_mcp_configs(self.home, self.cwd)
            self.assertIn(self.cwd / ".codex" / "config.toml", cfgs)
            self.assertIn(custom_codex / "config.toml", cfgs)
            self.assertNotIn(self.home / ".codex" / "config.toml", cfgs)

    def test_toml_skipped_when_tomllib_is_none(self):
        cfg = self.tmp_dir / "config.toml"
        cfg.write_text("[mcp_servers.tool]\ncommand = 'echo'\n", encoding="utf-8")
        with mock.patch.object(scan_inventory, "tomllib", None):
            out = scan_mcp_configs([cfg])
            self.assertEqual(out, [])


@unittest.skipIf(tomllib is None, "stdlib tomllib unavailable on Python < 3.11")
class TestCodexTomlParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.home = self.tmp_dir / "home"
        self.cwd = self.tmp_dir / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()

    def test_parse_mcp_servers_command_and_args(self):
        cfg = self.cwd / "config.toml"
        cfg.write_text(
            """
[mcp_servers.sqlite_server]
command = "uvx"
args = ["mcp-server-sqlite", "--db-path", "/tmp/test.db"]
""",
            encoding="utf-8",
        )
        out = scan_mcp_configs([cfg])
        self.assertEqual(len(out), 1)
        srv = out[0]
        self.assertEqual(srv["name"], "sqlite_server")
        self.assertEqual(srv["type"], "mcp_server")
        self.assertEqual(srv["source_config"], str(cfg))
        self.assertFalse(srv["external_mutation_risk"])

    def test_parse_mcp_servers_url(self):
        cfg = self.cwd / "config.toml"
        cfg.write_text(
            """
[mcp_servers.remote_kling]
url = "https://kling.ai/api/mcp"
""",
            encoding="utf-8",
        )
        out = scan_mcp_configs([cfg])
        self.assertEqual(len(out), 1)
        srv = out[0]
        self.assertEqual(srv["name"], "remote_kling")
        self.assertEqual(srv["default_gate"], "cost_confirm")
        self.assertTrue(srv["cost_sensitive"])

    def test_parse_multiple_servers_and_url_with_command(self):
        cfg = self.cwd / "config.toml"
        cfg.write_text(
            """
[mcp_servers.deploy_runner]
command = "deploy_prod"
args = ["--release"]

[mcp_servers.plain_service]
url = "http://localhost:8080/sse"
""",
            encoding="utf-8",
        )
        out = scan_mcp_configs([cfg])
        by_name = {s["name"]: s for s in out}
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["deploy_runner"]["default_gate"], "high_risk_confirm")
        self.assertTrue(by_name["deploy_runner"]["external_mutation_risk"])
        self.assertEqual(by_name["plain_service"]["default_gate"], "probe_required")

    def test_project_and_home_codex_configs(self):
        project_cfg = self.cwd / ".codex" / "config.toml"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(
            """
[mcp_servers.common_tool]
command = "project_version"

[mcp_servers.project_tool]
command = "proj_cmd"
""",
            encoding="utf-8",
        )

        home_cfg = self.home / ".codex" / "config.toml"
        home_cfg.parent.mkdir(parents=True)
        home_cfg.write_text(
            """
[mcp_servers.common_tool]
command = "user_version"

[mcp_servers.user_tool]
command = "user_cmd"
""",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            cfgs = default_mcp_configs(self.home, self.cwd)
            out = scan_mcp_configs(cfgs)
            by_name = {s["name"]: s for s in out}
            self.assertIn("project_tool", by_name)
            self.assertIn("user_tool", by_name)
            self.assertIn("common_tool", by_name)
            # Project-level config takes precedence
            self.assertEqual(by_name["common_tool"]["source_config"], str(project_cfg))

    def test_env_secrets_never_appear_in_output_and_not_classified(self):
        cfg = self.cwd / "config.toml"
        secret_token = "sk-super-secret-codex-token-12345"
        danger_command = "drop database prod_customer_records"
        cfg.write_text(
            f"""
[mcp_servers.harmless_reporter]
command = "safe_reporter"
args = ["--format", "json"]
env = {{ CODEX_API_KEY = "{secret_token}", DANGER_FLAG = "{danger_command}" }}
""",
            encoding="utf-8",
        )
        out = scan_mcp_configs([cfg])
        self.assertEqual(len(out), 1)
        srv = out[0]
        self.assertEqual(srv["name"], "harmless_reporter")

        # Serialized output must not contain secrets or sensitive env values
        dumped = json.dumps(out)
        self.assertNotIn(secret_token, dumped)
        self.assertNotIn("CODEX_API_KEY", dumped)
        self.assertNotIn(danger_command, dumped)

        # Destructive phrase inside env must NOT trigger external_mutation_risk or high_risk_confirm
        self.assertFalse(srv["external_mutation_risk"])
        self.assertNotEqual(srv["default_gate"], "high_risk_confirm")
        self.assertEqual(srv["evidence"]["risk"], [])

    def test_http_auth_fields_never_leak(self):
        cfg = self.cwd / "config.toml"
        cfg.write_text(
            """
[mcp_servers.figma]
url = "https://mcp.figma.com/mcp?token=QUERYSECRET"
bearer_token_env_var = "FIGMA_OAUTH_TOKEN"
http_headers = { "X-Api-Key" = "HEADERSECRET-delete-everything" }
env_http_headers = { "X-Other" = "OTHER_ENV_NAME" }

[mcp_servers.figma.oauth]
client_id = "cid"
client_secret = "OAUTHSECRET"

[mcp_servers.ctx]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
env_vars = ["LOCAL_TOKEN"]

[mcp_servers.ctx.env]
MY_ENV_VAR = "ENVSECRET-force-push"
""",
            encoding="utf-8",
        )
        out = scan_mcp_configs([cfg])
        self.assertEqual({s["name"] for s in out}, {"figma", "ctx"})
        dumped = json.dumps(out)
        for needle in ["QUERYSECRET", "FIGMA_OAUTH_TOKEN", "HEADERSECRET", "OTHER_ENV_NAME",
                       "OAUTHSECRET", "LOCAL_TOKEN", "ENVSECRET"]:
            self.assertNotIn(needle, dumped)
        for srv in out:
            self.assertFalse(srv["external_mutation_risk"], srv)

    def test_malformed_toml_skipped_gracefully(self):
        bad_cfg = self.cwd / "config.toml"
        bad_cfg.write_text("[mcp_servers.broken\ncommand = ", encoding="utf-8")
        out = scan_mcp_configs([bad_cfg])
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
