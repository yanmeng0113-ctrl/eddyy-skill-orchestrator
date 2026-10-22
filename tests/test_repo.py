#!/usr/bin/env python3
"""Repository-level checks: SKILL.md contract, links, evals, installer."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scan_inventory import parse_frontmatter  # noqa: E402

SKILL_MD = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")


class TestSkillMd(unittest.TestCase):
    def test_frontmatter_contract(self):
        self.assertTrue(SKILL_MD.startswith("---\nname: eddyy-skill-orchestrator\n"))
        fm = parse_frontmatter(SKILL_MD)
        self.assertEqual(fm["name"], "eddyy-skill-orchestrator")
        desc = fm["description"]
        self.assertTrue(desc.strip())
        self.assertLessEqual(len(desc), 1024, "description exceeds the 1024-character limit")
        self.assertNotRegex(desc, r"[<>]")

    def test_description_states_when_not_to_use_it(self):
        desc = parse_frontmatter(SKILL_MD)["description"]
        for needle in ("Do not use", "[no-router]", "single tool"):
            self.assertIn(needle, desc)

    def test_stays_short(self):
        self.assertLess(len(SKILL_MD.splitlines()), 150, "keep SKILL.md short; move detail to references/")

    def test_no_rewritten_search_urls(self):
        self.assertNotIn("google.com/search", SKILL_MD)

    def test_gate_vocabulary_matches_the_validator(self):
        for gate in ("auto", "cost_confirm", "high_risk_confirm"):
            self.assertIn(f"`{gate}`", SKILL_MD)


class TestLinks(unittest.TestCase):
    PATH_RE = re.compile(r"`((?:references|assets|scripts|evals|tests)/[\w./-]+\.\w+)`")

    def test_every_referenced_file_exists(self):
        for doc in ("SKILL.md", "README.md", "CONTRIBUTING.md"):
            text = (REPO_ROOT / doc).read_text(encoding="utf-8")
            for rel in self.PATH_RE.findall(text):
                with self.subTest(doc=doc, path=rel):
                    self.assertTrue((REPO_ROOT / rel).exists(), f"{doc} mentions missing file {rel}")

    def test_every_script_is_documented_in_skill_md(self):
        for script in (REPO_ROOT / "scripts").glob("*.py"):
            with self.subTest(script=script.name):
                self.assertIn(script.name, SKILL_MD)

    def test_readme_has_no_static_ci_badge(self):
        self.assertNotIn("CI_Tests-Passing", (REPO_ROOT / "README.md").read_text(encoding="utf-8"))


class TestEvals(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((REPO_ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.evals = self.data["evals"]

    def test_shape(self):
        self.assertEqual(self.data["skill_name"], "eddyy-skill-orchestrator")
        ids = [e["id"] for e in self.evals]
        self.assertEqual(len(ids), len(set(ids)), "duplicate eval ids")
        for e in self.evals:
            with self.subTest(eval=e["id"]):
                for key in ("eval_name", "prompt", "should_trigger", "expected_output", "assertions"):
                    self.assertIn(key, e)
                self.assertIsInstance(e["should_trigger"], bool)
                self.assertTrue(e["assertions"])

    def test_has_near_miss_negatives(self):
        negatives = [e for e in self.evals if not e["should_trigger"]]
        self.assertGreaterEqual(len(negatives), 4)
        self.assertTrue(any(e["eval_name"].startswith("near-miss") for e in negatives))

    def test_readme_eval_count_is_accurate(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(self.evals)} behavioural evals", readme)


def _find_bash() -> str | None:
    """Locate a usable bash.

    On Windows ``shutil.which("bash")`` resolves to the WSL shim
    (``C:\\Windows\\System32\\bash.exe``), which fails without a distro and
    prints GBK-localised errors. Prefer Git Bash explicitly there.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    # git --exec-path points at .../mingw64/libexec/git-core; bash lives in ../bin
    try:
        r = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            candidates.append(str(Path(r.stdout.strip()).parent / "bin" / "bash.exe"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return shutil.which("bash")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess with UTF-8 decoding so GBK console output cannot crash."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@unittest.skipUnless(_find_bash(), "bash not available")
class TestInstaller(unittest.TestCase):
    def _bash(self) -> str:
        return _find_bash()

    def test_syntax(self):
        r = _run([self._bash(), "-n", str(REPO_ROOT / "install.sh")])
        self.assertEqual(r.returncode, 0, (r.stderr or "") + (r.stdout or ""))

    def test_install_into_explicit_target(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = _run([self._bash(), str(REPO_ROOT / "install.sh"), "--target", tmp, "--no-scan"])
            self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
            dest = Path(tmp) / "eddyy-skill-orchestrator"
            for rel in ("SKILL.md", "references/tier-mapping-guide.md", "assets/pipeline-schema.json",
                        "scripts/scan_inventory.py", "scripts/validate_pipeline.py", "scripts/checkpoint_state.py"):
                self.assertTrue((dest / rel).exists(), rel)
            self.assertFalse((dest / "evals").exists())
            self.assertFalse(list((dest / "scripts").glob("__pycache__")))

    def test_upgrade_keeps_previous_install_and_deletes_nothing(self):
        """Upgrades must not run rm: agent hosts intercept rm behind an approval
        prompt, and a blocked install leaves the user worse off than a leftover
        directory. The previous install is renamed and kept instead."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            first = _run([self._bash(), str(REPO_ROOT / "install.sh"), "--target", tmp, "--no-scan"])
            self.assertEqual(first.returncode, 0, (first.stdout or "") + (first.stderr or ""))
            second = _run([self._bash(), str(REPO_ROOT / "install.sh"), "--target", tmp, "--no-scan"])
            self.assertEqual(second.returncode, 0, (second.stdout or "") + (second.stderr or ""))
            dest = Path(tmp) / "eddyy-skill-orchestrator"
            self.assertTrue((dest / "SKILL.md").exists())
            kept = [p for p in Path(tmp).iterdir() if p.name.startswith("eddyy-skill-orchestrator.old-")]
            self.assertTrue(kept, "previous install was not kept")
            self.assertTrue((kept[0] / "SKILL.md").exists())
            self.assertNotIn("rm ", (REPO_ROOT / "install.sh").read_text(encoding="utf-8").replace("rm -rf", ""))


if __name__ == "__main__":
    unittest.main()
