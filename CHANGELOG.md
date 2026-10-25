# Changelog

## 0.5.1

Skill scanner follows directory symlinks (common custom-skills installs), de-duplicates by resolved path, and refuses to hang on symlink loops. Adds three regression tests.

## 0.5.0

Node-level DAG checkpoints, verified tool status, fallback switching, and real artifact delivery acceptance.

**Scanner (`scan_inventory.py`)**
- Each skill and MCP entry now provides structured metadata: `inputs`, `outputs`, `cost` (`"free"` / `"paid"` / `"unknown"`), `permissions` (risk signals e.g. `destructive`, `external`, `paid`), and `status`.
- Tool readiness states: `status` defaults to `"configured"`, marks `"unavailable"` if commands are missing or probes fail, and transitions to `"verified"` only via `mark_verified(entry, probe_ok)` or CLI option `--verified NAME`.
- Added helper functions `mark_verified`, `mark_unavailable`, `is_verified`, `is_available`, `detect_cost`, and `detect_permissions`.

**Checkpoint State (`checkpoint_state.py`)**
- Upgraded schema to version 3 with node-level state tracking: `state["nodes"]` tracks node statuses (`pending`, `in_progress`, `completed`, `failed`, `needs_rerun`), produced artifacts, DAG dependencies, primary tool, and fallbacks.
- New commands: `node-add`, `node-complete`, `node-fail`, `rerun` (invalidates node and transitive downstream nodes while preserving unrelated nodes), `next-tool` (picks first available tool from primary + fallbacks), and `budget` (exits 3 on budget overrun).
- Seamless backward compatibility: legacy tier-based checkpoint files (schema version 1/2) automatically migrate to node-level representation on load.

**Pipeline Validator & Schema (`pipeline-schema.json`, `validate_pipeline.py`)**
- Tier definitions support optional `node_id` and `fallback` arrays.
- `depends_on` supports referencing upstream node IDs in addition to tier IDs.

**Skill & Workflow Rules (`SKILL.md`)**
- Route cards must flag unverified tools; paid and high-risk steps prefer verified tools.
- Automated fallback switching after 2 retries, stopping immediately if budget or authorization is exceeded.
- Delivery acceptance requires inspecting real artifacts against L1 specifications.

## 0.4.0

OpenAI Codex support.

**Scanner (`scan_inventory.py`)**
- Default skill roots follow the Codex docs (developers.openai.com/codex/skills, checked 2026-10-22): `.agents/skills` in every directory from cwd up to the git root (REPO scope), `~/.agents/skills` (USER, already scanned), `/etc/codex/skills` (ADMIN).
- Also `$CODEX_HOME/skills` (default `~/.codex/skills`; empty `CODEX_HOME` falls back), which holds Codex's bundled `.system` skills and is where its skill-installer writes.
- MCP scanning reads Codex TOML configs: `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`) and project-level `./.codex/config.toml`, parsing `[mcp_servers.<name>]` tables. Same rule as JSON: only name, `command`, `args` and URL host are classified; `env`/headers never reach classification or output.
- Uses stdlib `tomllib` (Python 3.11+); on older Pythons or on a parse error the TOML file is skipped gracefully.
- MCP entries whose config is not a table/object are now skipped.

**Tests**
- New `tests/test_codex.py`: Codex skills dir, `CODEX_HOME`, TOML parsing (command/args/url, project vs user precedence, malformed file, `tomllib` missing) an `env` secret that must not leak into output, and HTTP auth fields (`http_headers`, `env_http_headers`, `bearer_token_env_var`, `oauth`, `env_vars`, URL query) that must not leak either. Repo-scope and admin roots.

## 0.3.0

Fixes found by deploying 0.2.0 on a real host (Windows host with an agent session).

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
