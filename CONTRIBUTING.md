# Contributing to `eddyy-skill-orchestrator` (贡献指南)

## Teach the scanner a new tool or domain (新增工具识别规则)

All vocabulary lives in `scripts/scan_inventory.py`. Patterns match **normalised** text: it is lower-cased and `_ - . / :` become spaces, so write `generate video`, not `generate_video`.

1. Pick the list:
   - `_TIER_TERMS`: tier vocabulary for `L0-Ambient`, `L1`, `L2a`, `L2b`, `L3`, `L4`.
   - `_DESTRUCTIVE_*`: irreversible whatever the wording (never downgraded by "read-only").
   - `_EXTERNAL_*`: visible outside the workspace or spends real money (a clear "read-only" statement downgrades them).
   - `_COST_STRONG_*`: paid services; safe to apply to tool names. `_COST_HINT_*`: only meaningful in prose (credit, balance).
   - `_ZH_NEUTRAL`: Chinese words that look risky but are ordinary nouns (发布会, 线上线下 ...).
2. Prefer verb-object phrases (`publish to`, `drop table`, `发布到`) over bare words that also appear in harmless contexts.
3. Add a regression test in `tests/test_classifier.py`: one case that must be flagged and, where relevant, one near-miss that must not be.
4. Run the tests on the oldest supported Python (3.9) and a current one.

## Other changes

- `scripts/validate_pipeline.py` constants (`REQUIRED_TOP`, `REQUIRED_TIER`, gate and mode enums) must match `assets/pipeline-schema.json`. `tests/test_validator.py` fails if they drift.
- Keep `SKILL.md` short (the tests cap it at 150 lines and the description at 1024 characters). Put detail in `references/`, and state each rule in one place only.
- New behaviour belongs in `evals/evals.json` too. Add a near-miss negative whenever you widen what the skill triggers on.
- Scripts stay standard-library only and compatible with Python 3.9 (`from __future__ import annotations`, no `match`, no runtime `X | Y`).

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Releasing this repo (发布到 GitHub)

1. Replace `yanmeng0113-ctrl` in `README.md` (2 occurrences: the clone URL and the `npx skills add` line) with your GitHub account name.
2. Run the suite once more: `python3 -m unittest discover -s tests -p "test_*.py" -v` (80 tests, all green) and `bash -n install.sh`.
3. Commit, tag and publish:

```bash
git add -A && git commit -m "release: v0.5.0"
git tag -a v0.5.0 -m "v0.5.0"
git push origin main --tags
```

4. Create the GitHub Release from the tag and attach the packaged zip, so users can install without cloning.
5. Move the "Unreleased" heading in `CHANGELOG.md` to the released version number, and open a new "Unreleased" section above it.

### Windows notes for contributors

- The tests locate **Git Bash** explicitly (`C:\Program Files\Git\bin\bash.exe`); `shutil.which("bash")` finds the WSL shim, which fails without a distro.
- `install.sh` performs **no deletions**: an upgrade stages the new version, renames the previous one to `<dest>.old-<timestamp>` and moves the staged copy into place. Keep it that way — hosts that put `rm` behind an approval prompt turn a reinstall into a hang. A regression test asserts `install.sh` contains no `rm`.
