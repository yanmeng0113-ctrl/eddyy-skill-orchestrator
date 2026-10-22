# Tier Mapping Guide (跨领域分级、嵌套与握手指南)

Read this when you map an unfamiliar domain or composite MCP, nest a pipeline inside a larger one, or need the pass criteria between tiers. The tier table and the gate rules live in `SKILL.md`; they are not repeated here.

## Contents

1. Classifying an unfamiliar tool
2. Cross-domain matrix
3. Splitting composite MCP servers
4. L2 waves
5. Nesting a pipeline
6. Handshake criteria
7. Gate edge cases

## 1. Classifying an unfamiliar tool (未知工具判定)

Check three signals in order. They are hints that need a judgement call, not rules to apply blindly.

1. **Input/output contract**
   - Input is raw intent or a web query, output is a plan/spec/script: **L1**.
   - Input is the L1 spec, output is something later steps must reuse unchanged (3-view sheet, seed, schema, voice clone): **L2a**.
   - Input is L1 + L2a, output is an atomic media/code/data asset: **L2b**.
   - Input is several L2 assets, output is one aligned structure (timeline, integration): **L3**.
   - Input is an L3 structure, output is a heavy compiled artifact (video, binary, report): **L4**.
   - Output changes state outside the workspace, spends money, or publishes: **L5**.
2. **Parameter schema.** Parameters such as `price`, `budget`, `credit`, `model="pro"`, `duration_seconds`, `resolution="4k"` suggest a paid call. Parameters such as `deploy`, `publish`, `public`, `delete`, `drop`, `overwrite` suggest an external or destructive effect. `recipient` and `channel` only count together with a write verb: a Slack tool that lists channels is a read.
3. **Action verb.** `get_*`, `list_*`, `search_*`, `read_*`, `analyze_*` are reads. `generate_*`, `create_draft_*`, `render_*`, `build_*` are internal synthesis (L2 to L4). `send_*`, `post_*`, `publish_*`, `deploy_*`, `delete_*`, `pay_*` are external mutation.

If the signals disagree or the schema is unavailable, treat the tool as `high_risk_confirm`. `scripts/scan_inventory.py --classify "<text>"` applies the same logic to a description and reports `probe_required` when it recognises nothing.

## 2. Cross-domain matrix (六大领域对照表)

| Tier | AIGC video & audio | Software engineering | Data & BI | Research & office automation | Design & 3D | Enterprise campaign |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| **L1** | Script, scene breakdown, character bio, style guide | PRD, architecture, OpenAPI spec, DB schema | Hypothesis, metric definitions, query plan | Search/RAG discovery (`brave`, `exa`, `firecrawl` read), outline | Design-system tokens, wireframe spec, moodboard | Positioning, budget allocation, channel strategy |
| **L2a** | Character 3-view (`三视图`), voice clone seed, LoRA lock | Core data models (`prisma`), shared types, auth middleware | Raw SQL extraction, schema cleaning | Entity extraction, canonical dataset lock | Component primitives, 3D base mesh and rig | Brand identity kit, core messaging pillars |
| **L2b** | Scene frames (`文生图`), TTS clips, SFX/music | Feature components, route handlers, unit tests | Feature tables, chart plots | Drafted sections, translations, scraped item cards | High-res UI screens, UV textures, animations | Channel-specific copy, raw graphic assets |
| **L3** | Storyboard timeline (`故事板`), lip-sync, camera alignment | Frontend/backend integration, E2E tests | Dashboard layout, multi-chart alignment | Cross-document synthesis, doc draft assembly | Prototype linking, 3D scene lighting | **Sub-pipeline**: full video production (L1-L4 nested) |
| **L4** | Image-to-video render, FFmpeg mastering | Production bundle build, Docker image | Final PDF/PPTX/XLSX compilation | Full browser-workflow dry run | Final 3D render, asset export | Multi-platform bundle and compliance check |
| **L5** | Upload to YouTube, TikTok, Bilibili, Xiaohongshu | Terraform apply, prod deploy, merge or push to the default branch | Email blast, live warehouse write-back | Send Slack/Gmail messages, submit external forms | Publish to a community or live CDN | Live ad spend, public social publishing |

## 3. Splitting composite MCP servers (复合型 MCP 按动作拆分)

Never give a whole server one tier. Classify each function.

- **github**: `get_file_contents`, `search_repositories`, `list_issues` are L1/L2a and `auto`. `create_branch`, `create_or_update_file` on a feature branch are L2b/L3 and `auto`. `merge_pull_request`, `delete_file`, `create_release`, or any write to the default branch are L5 and `high_risk_confirm`.
- **supabase / postgres**: `list_tables`, `execute_readonly_sql` are L1/L2a and `auto`. `apply_migration`, `delete_records`, `drop_table` are L5 and `high_risk_confirm`.
- **notion / google-workspace**: search and read are L1; creating or editing a private draft is L2b/L3; sharing, sending or publishing to others is L5.
- **studio MCPs (kling, kie-ai, comfyui, figma)**: setup and variables are L2a; image/TTS/component generation is L2b; lip-sync, auto-layout and effects are L3; video rendering is L4 with `cost_confirm`.

## 4. L2 waves (L2 同级先后锁)

- **Wave L2a**: run tools that establish seeds, turnarounds, schemas or shared types first, and lock their outputs.
- **Wave L2b**: pass the locked reference (image ID, seed, interface file) as a strict input to every L2b tool. Scenes 1, 2 and 3 can then run in parallel.

## 5. Nesting a pipeline (子工作流嵌套)

For a macro request ("research, produce and launch a campaign"), a domain pipeline that would span L1 to L4 on its own becomes one **L3 node** of the parent.

1. The parent plans `L3 [Sub-Pipeline: Video Production]`.
2. When execution reaches it, run the micro-pipeline `L3.1 Script -> L3.2a 3-View -> L3.2b Frames -> L3.3 Storyboard -> L3.4 Render`. Use these nested ids with `checkpoint_state.py`.
3. Gates and estimated costs inside the nested pipeline bubble up: the parent tier's gate must be at least as strict as the strictest inner gate, and its estimate at least the inner total. In a plan file this is the tier's `sub_pipeline` field, and `validate_pipeline.py` enforces both.

## 6. Handshake criteria (上下游自检)

Run silently after each tier. Continue only if it passes; otherwise roll back that node (maximum 2 retries, then escalate).

- **L1 to L2**: the blueprint names its entities, style constraints and scene/module counts.
- **L2a to L2b**: the anchor exists and exposes a reusable reference (image URI, seed, schema path).
- **L2b to L3**: every required atomic asset exists and matches the count and specs in L1.
- **L3 to L4**: the timeline or integration test has no missing references.
- **L4 to L5**: the deliverable opens, has the expected format/duration/size, and matches the L1 constraints. Only then ask for the `high_risk_confirm`.

## 7. Gate edge cases (门控边界示例)

- Push to a feature branch: `auto`. Push or merge to the default branch, or any force-push: `high_risk_confirm`.
- Local FFmpeg render, local build, local test run: `auto`, even though they sit in L4.
- A cheap paid call (a TTS clip, a single image): still `cost_confirm`, but it is covered silently when the user pre-approved a budget that fits the cumulative estimate.
- Paid status or balance check on a paid service: free read, `auto`.
- Deleting a scratch file or a node you created in this session: `auto`. Deleting anything you did not create, or anything in production: `high_risk_confirm`.
- Dry run or preview of a deploy or send: `auto`; the real action behind it is `high_risk_confirm`.
