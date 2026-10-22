# Changelog

## 0.3.0

Fixes found by deploying 0.2.0 on a real host (Windows + WorkBuddy session).

**Scanner (`scan_inventory.py`)**
- Default roots now include `~/.workbuddy/plugins/cache` and `~/.cursor/extensions`: host built-in skills often live in a versioned plugin cache rather than `~/.workbuddy/skills` (this alone missed 22 of 25 session skills during deployment).
- End-of-scan stderr summary: roots present but empty, duplicate names (kept vs shadowed path), and the full `probe_required` list.
- `L0_SKILL_ROOTS` is now documented in `README.md`, `SKILL.md` and the `install.sh` output (previously it existed only in the source).
- Tier vocabulary: `browser automation` added to L1.

**Installer (`install.sh`)**
- **Upgrades no longer delete anything.** 0.2.0 refreshed the install with `rm -rf` on `references/`, `assets/`, `scripts/`, `evals/`. Agent hosts commonly intercept `rm` behind an approval prompt (WorkBuddy ships a `safe-bin/rm` shim), so a reinstall blocked on a delete instead of installing — a broken upgrade in exactly the environment that runs the skill. The installer now stages the new version, renames the previous install to `<dest>.old-<timestamp>` and moves the staged copy into place: no `rm`, no approval, and the old version is still there to roll back to.
- `L0_SKILL_DIRS` separator follows the platform (`;` on MSYS/Git-for-Windows, `:` elsewhere) so Windows drive letters survive (previously `D:\a:D:\b` was split into `D`, `\a`, ... and created a directory at the drive root).
- Auto-detection only adds agent directories that already exist; with none found it exits with guidance instead of creating empty dirs.
- Rejects unsafe `--target` values (empty, `/`, `/x`, `C:`).
- Falls back to `python` when `python3` is missing (Windows); prints the `L0_SKILL_ROOTS` hint after the scan.
- Scripts are copied file by file, so a `__pycache__` in the source tree can no longer reach the install.

**Tests (`tests/test_repo.py`)**
- Resolves Git Bash explicitly on Windows (`shutil.which("bash")` found the WSL shim, whose GBK-localised errors also crashed `subprocess.run(..., text=True)`); all subprocess calls now decode UTF-8 with `errors="replace"`. CI still runs on ubuntu only.

## 0.2.0

Safety fixes, found by reproducing the findings of two independent reviews and our own audit.

**Scanner (`scan_inventory.py`)**
- Fixed the root cause of the fail-open classifier: `\b` treats `_` as a word character, so `stripe_payment` or `deploy_prod` never matched. Text is now normalised first.
- Much wider risk vocabulary (force-push, `rm -rf`, `drop database`, `reset --hard`, transfer funds, purchase, buy, send email, merge pull request, permissions ...), English and Chinese.
- Unrecognised tools now return `default_gate: probe_required` and `needs_probe: true` instead of `L2b` + `auto`.
- Fewer Chinese false positives: neutral compounds (发布会, 线上线下, 支付宝, 购买力 ...) are ignored, social platforms only count together with a publishing verb, and an explicit "read-only / 只读" downgrades the weak external terms (never the destructive ones).
- `env`, `headers` and other secrets from MCP configs no longer influence classification; per-project servers in `~/.claude.json` are scanned.
- Multi-line YAML descriptions (`>` and `|`) are parsed; CRLF and BOM handled.
- Python 3.9 compatible (it crashed on 3.9 before). New `--classify` mode.

**Validator (`validate_pipeline.py`)**
- Paid work under `auto` is rejected unless its estimate fits a user-stated `approved_budget_usd` (previously only L5 was checked).
- Tool names signalling external or destructive actions must be `high_risk_confirm` on any tier; `L5` must be `high_risk_confirm`.
- Dependencies and rollback targets must exist in the plan (or in `satisfied_tiers`); duplicate tiers, bad order, wrong types and missing required fields are reported instead of crashing or passing.
- Sub-pipeline gates and costs bubble up. Required fields now match the schema, and a test keeps them in sync.

**Checkpoint (`checkpoint_state.py`)**
- A corrupt state file is no longer silently reset (it used to drop all locked anchors on the next write): the command fails with exit 4 and keeps a copy.
- Atomic writes; tier names validated; `init` will not replace an in-progress pipeline; locked anchors are not overwritten without `--force`.
- New `rollback` (counts retries, stops at the limit with exit 3), `confirm` (audit trail for gates) and `show --brief` (resume summary).

**Skill content**
- `SKILL.md`: 113 to 84 lines. Narrower trigger description with explicit non-triggers; light mode; one gate rule decided by action; user-approved budget; missing-tool fallback; bounded retries; the host's permission prompts take precedence.
- Removed text that duplicated the description, the all-lines bilingual repetition and the unsupported "95%" claim; the L2a/L2b rule now lives in one place.
- `references/tier-mapping-guide.md`: resolved the "any paid call" vs "above a threshold" contradiction, added gate edge cases.
- Evals: 6 to 14, including near-miss negatives, budget pre-approval, rollback limit, resume and read-only handling.

**Repo**
- Removed the static "CI passing" badge. CI now runs on Python 3.9, 3.11 and 3.13 and smoke-tests all three scripts.
- `install.sh`: `--target`, `$L0_SKILL_DIRS`, WorkBuddy detection (path not verified), no longer copies `evals/`, no longer hides scan failures.
