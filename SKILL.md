---
name: eddyy-skill-orchestrator
description: Plan and run multi-stage pipelines that chain several Skills or MCP tools in dependency order, with a route card, locked anchors (seeds, schemas, reference images) and cost/risk confirmation gates. Use when a task needs three or more dependent tool stages (e.g. script, then character sheet, keyframes, storyboard, a paid video render and publishing), when later stages must inherit locked outputs from earlier ones, or when any stage spends money or publishes, deploys or deletes outside the workspace; also when the user asks to tier, route or orchestrate their skills and MCP tools (分级, 编排, 调度, 流水线). Do not use for single tool calls, two-step linear chains, factual questions, brainstorming, or when the prompt contains [no-router].
---
# Eddyy Skill Orchestrator (L0 全局技能与 MCP 总控调度)

Plans and runs multi-tool pipelines: it fixes the order, locks what must stay consistent, and pauses only where money or irreversible effects are involved. It adds planning discipline; it never replaces the host's own permission prompts and never overrides the user's instructions. `scripts/` and `references/` are optional accelerators: if they are missing, apply the same rules by hand. Reply in the user's language.

## When to use it

- **Full mode**: three or more dependent stages, or any stage that spends money or changes something outside the workspace.
- **Light mode**: two linear stages with nothing paid or external. Say the plan in one line and do it. No route card, no scripts.
- **Off**: the prompt contains `[no-router]`, or it is a single tool call, a question, or a brainstorm.

## Tiers (分级)

A tier is the *role* a step plays in the current task, not a fixed property of a tool. Recompute it for every request: inside a larger launch campaign, a whole video pipeline collapses into one L3 node (see the reference, "Nesting").

| Tier | Role | Examples |
| :-- | :-- | :-- |
| **L0-Ambient** 全局修饰 | Rules for the whole session. Bind them everywhere; never schedule them as a step. | tone/brevity skills, design or code style guides |
| **L1** 规划 | Blueprint, research, interview, spec. No upstream dependency; constrains everything below. | script, PRD, web research, architecture choice |
| **L2a** 锚点 | Anchors that must stay identical downstream. Always run before L2b. | character 3-view + seed, design tokens, DB schema, voice clone |
| **L2b** 衍生 | Assets derived from L1 + L2a. May run in parallel with each other, never before L2a is locked. | keyframes, TTS, UI components, feature modules |
| **L3** 组装 | Combine L2 assets into one aligned structure. | storyboard timeline, lip-sync, auto-layout, integration tests |
| **L4** 合成 | Heavy synthesis or compilation. | video render, full build, final PDF/PPTX export |
| **L5** 交付 | Changes something outside the workspace or cannot be undone. | publish, deploy, send, merge to default branch, delete, payment |

## Procedure

1. **Inventory.** Use the skills and tools already visible in this session; run `python3 scripts/scan_inventory.py` only in large workspaces. Classify by what a tool *does*. Split composite servers (github, supabase, figma, studio-style MCPs such as kling) per action, never per server. For an unfamiliar tool read its description and schema; if they are not available run `scan_inventory.py --classify "<name> <description>"`. A tool you still cannot judge is treated as `high_risk_confirm`.
2. **Entry point.** Mark upstream outputs the user already supplied as satisfied and start at the first open tier. If `.l0-state.json` exists, run `python3 scripts/checkpoint_state.py show --brief` and resume from it.
3. **Route card.** Print it before the first tool call (template below) with the total estimated cost. The route card must flag tools not verified; paid/high-risk steps prefer verified tools. The user may pre-approve a budget at any time; without one you ask at the first paid step. Only a budget the user actually stated counts. If a tier has no suitable tool: do it yourself with native abilities, skip it when optional, or ask once. Never invent a tool. For large or costly plans, write the plan as JSON (`assets/pipeline-schema.json`, example in `assets/example-pipeline.json`) and run `python3 scripts/validate_pipeline.py plan.json` first.
4. **Run L1 to L4 without asking "continue?"** on safe steps. When L2a finishes, record the anchors (`checkpoint_state.py lock`) and pass them verbatim into every L2b/L3 call. After each tier, check its output against the L1 blueprint (criteria in the reference, "Handshake").
5. **On a defect**, roll back only the faulty node (`checkpoint_state.py rerun --id X`), adjust, and re-run just that branch. Touch L1 only if the requirements changed. At most 2 retries per node; after 2 retries switch to fallback tool (`checkpoint_state.py next-tool`), and stop to confirm immediately if it would exceed authorization or budget (`checkpoint_state.py budget`). After retry limit, stop and tell the user what failed and what you tried. A timeout or an error after a call was sent does not prove the step did not run: before retrying any step with external effects, check its state first (`checkpoint_state.py show`, the tool's own status, or the produced artifact), because the first attempt may already have succeeded. Never repeat an unchanged call whose outcome is unknown — re-check, then decide.
6. **Delivery acceptance.** Check the real artifacts meet L1 requirements — a successful tool call is not task completion. Before declaring the pipeline done, sweep the route card once more: every tier is marked done, partial or unresolved, and every unresolved item is named in the final message with its evidence status (below). A pipeline with an unresolved tier is reported as partial, never as complete.
7. **Gates.** Apply the table below before every action.

## Evidence discipline

Label every result the plan relies on, and keep the label attached when you report it:

- `observed` — you inspected the artifact or output yourself.
- `reported` — a tool or subagent claims it; not yet inspected.
- `inferred` — deduced from indirect signals.

A handshake tick for a tier feeding L5 needs `observed`; mid-pipeline, `reported` is enough to proceed, but it must be upgraded to `observed` at delivery acceptance. Never present `reported` or `inferred` as `observed`; if a check could not be performed, say which one and why. Bound every negative result: report what was actually searched (roots, query, scope) next to "not found" — an empty result inside a bounded search is not proof of absence outside it. Work summary-first: when a stage returns more output than the next decision needs, keep the summary plus the artifact reference (checkpoint path, result file) and drill in only on a specific question; do not pour bulk output into the plan or the user message.

## Gates (熔断)

Decide per action, by what it does, not by the tier it sits in.

| Gate | When | Behaviour |
| :-- | :-- | :-- |
| `auto` | Reversible and internal: reads, drafts, local renders, writes to a feature branch or scratch space | Proceed |
| `cost_confirm` | Spends money or paid quota | State the exact estimate and ask go/no-go, unless the cumulative estimate so far fits the budget the user pre-approved. Check a balance tool first if one exists |
| `high_risk_confirm` | Irreversible or visible outside the workspace: publish, send, deploy, merge to the default branch, delete, overwrite, change permissions, pay | Always ask, even if a budget was approved |

- A tool that is verifiably read-only is `auto`, even when its server also exposes paid or destructive tools.
- Costs and gates inside a nested pipeline bubble up to its parent tier.
- Breaker message: completed tiers, the next action, exact cost or impact, one go/no-go question. For a configuration change, the breaker message lists the exact change set (files, keys, values) before anything is written, and a dry-run or diff is produced first when the tool supports one. Save state first (`checkpoint_state.py complete-tier ... --status awaiting_confirmation`); after the answer run `checkpoint_state.py confirm`.

## Route card

```text
[L0-Route-Card | 路由卡]
Scope 任务: <summary>   Entry 入口: L<N>   Budget 预算: <user-approved $ | none>   Est. 预估: $<n>
Ambient 全局修饰: <skills | None>
[ ] L1  规划  <tool> -> <output>                    Gate: auto
[ ] L2a 锚点  <tool> -> <output>                    Gate: auto
[ ] L2b 衍生  <tool> (needs L2a) -> <output>        Gate: auto
[ ] L3  组装  <tool> -> <output>                    Gate: auto
[ ] L4  合成  <tool> -> <output>                    Gate: cost_confirm ($<n>)
[ ] L5  交付  <tool> -> <output>                    Gate: high_risk_confirm
Satisfied by user 用户已提供: <tiers, marked [x] and skipped>
```

Tick `[x]` as tiers pass their handshake. Drop rows the task does not need.

## Gotchas

- **Static tier trap**: never pin a tool to one tier; recompute against the current scope.
- **Ambient is not a step**: tone, brevity and design-rule skills apply across all tiers.
- **Composite MCP blanket label**: split a studio or CRUD server across tiers by action.
- **L2 race**: never start L2b while L2a has not locked the identity.
- **Scanner output is a hint**: it keyword-matches names and descriptions. `probe_required` means "inspect this tool", not "safe".
- **Timeout ≠ not run**: a step that timed out may have completed. Verify state before any retry — above all before repeating a publish, send or payment.
- **Empty search ≠ absence**: state the searched scope with the negative result; "not in these roots" is all a scan can honestly claim.
- **Over-orchestrating**: a route card for a two-step task is noise; use light mode.

## Resources

- `references/tier-mapping-guide.md`: read when mapping an unfamiliar domain or composite MCP, nesting pipelines, or checking handshake criteria.
- `assets/pipeline-schema.json`, `assets/example-pipeline.json`: plan format for the validator.
- `scripts/scan_inventory.py [out.json]` and `--classify "<text>"`: advisory tier and gate hints. Reads the default roots (Claude Code, `~/.agents`, Cursor, WorkBuddy and OpenAI Codex: `.agents/skills` from cwd up to the git root, `~/.agents/skills`, `$CODEX_HOME/skills` (default `~/.codex/skills`), `/etc/codex/skills`; Codex MCP servers from `~/.codex/config.toml` and `./.codex/config.toml`) plus anything in `$L0_SKILL_ROOTS` (separator: `os.pathsep`); hosts whose skills are not on disk can be indexed via a directory of manifest stubs.
- `scripts/validate_pipeline.py <plan.json>`: checks order, dependencies, the L2a/L2b lock and the gates.
- `scripts/checkpoint_state.py init|lock|complete-tier|confirm|rollback|show|node-add|node-complete|node-fail|rerun|next-tool|budget`: checkpoint manager in `.l0-state.json`. New node commands: `node-add --id X [--depends-on A,B] [--tool T] [--fallback T2,T3]`, `node-complete --id X --artifact PATH`, `node-fail --id X`, `rerun --id X [--reason]` (clears node + downstream artifacts, respects max retries), `next-tool --id X --unavailable T,...` (selects first available tool), `budget --approved USD --spent USD --next-cost USD` (exits 3 on overrun).
