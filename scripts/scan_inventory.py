#!/usr/bin/env python3
"""Advisory inventory scanner for the L0 orchestrator (stdlib only, Python >= 3.9).

This is a keyword heuristic over names and descriptions. It produces HINTS for the
agent, never a safety guarantee. What it does:

  * Normalises text first (``stripe_payment`` -> ``stripe payment``) so that
    underscore / hyphen / dot identifiers match word-boundary patterns.
  * Detects destructive and externally visible actions (force-push, rm -rf, drop
    database, send email, transfer funds, purchase, ...) in English and Chinese.
  * Understands explicit read-only wording ("read-only", "只读") for the *weak*
    external-action terms, but never for destructive ones.
  * Treats unrecognised tools as ``probe_required`` instead of silently ``auto``.
  * Never feeds ``env`` / ``headers`` / secrets from MCP configs into classification.

Usage:
  scan_inventory.py [OUT.json]            scan local skills + MCP configs
  scan_inventory.py --classify "<text>"   classify one tool/skill description
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# Normalisation and pattern helpers
# --------------------------------------------------------------------------- #
_SEPARATORS = re.compile(r"[_\-./:\\|]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SUFFIX = r"(?:s|es|d|ed|ing)?"


def normalize(text: str, split_camel: bool = False) -> str:
    """Lower-case and turn identifier separators into spaces."""
    if split_camel:
        text = _CAMEL.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    return re.sub(r"\s+", " ", text).lower()


def _en(phrases: list[str]) -> re.Pattern:
    """Word-bounded, inflection-tolerant pattern over *normalised* English text.

    Literal spaces in a phrase match any whitespace run; ``" ?"`` makes the gap optional
    (``comfy ?ui`` matches both "comfyui" and "comfy ui"). Phrases may contain regex.
    """
    alts = "|".join(p.replace(" ?", r"\s*").replace(" ", r"\s+") for p in phrases)
    return re.compile(rf"\b(?:{alts}){_SUFFIX}\b")


def _zh(terms: list[str]) -> re.Pattern:
    """Chinese terms: substring match, entries may be regex fragments."""
    return re.compile("|".join(terms))


def _find(en: re.Pattern, zh: re.Pattern, norm: str, raw_zh: str) -> list[str]:
    hits = [m.group(0) for m in en.finditer(norm)] + [m.group(0) for m in zh.finditer(raw_zh)]
    seen: list[str] = []
    for h in hits:
        h = re.sub(r"\s+", " ", h)
        if h not in seen:
            seen.append(h)
    return seen


# --------------------------------------------------------------------------- #
# Tier vocabulary (L5 is derived from the risk lexicon below)
# --------------------------------------------------------------------------- #
_TIER_TERMS: dict[str, tuple[list[str], list[str]]] = {
    "L0-Ambient": (
        [r"voice\b.*\bmode", "talk like", "be brief", "less tokens", "terse",
         "design foundations", "interface design", "style guide", "coding standard",
         "coding convention", "persona", "tone"],
        ["语气", "文风", "人设风格", "全局规范"],
    ),
    "L1": (
        ["plan", "planning", "script", "screenplay", "architect", "architecture", "spec",
         "specification", "prd", "strategy", "outline", "schema", "research", "search",
         "brave", "tavily", "perplexity", "exa", "firecrawl", "crawl", "scrape", "audit",
         "brainstorm", "interview", "product selection", "lookup", "browser automation"],
        ["规划", "脚本", "剧本", "大纲", "立项", "调研", "检索", "搜索", "查询", "爬虫",
         "抓取", "架构", "需求", "设定", "世界观", "蓝图", "策划"],
    ),
    "L2a": (
        ["character sheet", "turnaround", "3 view", "three view", "seed", "lora",
         "style lock", "brand kit", "design token", "variables or tokens", "data model",
         "prisma", "drizzle", "voice clone", "avatar", "rig"],
        ["三视图", "角色设定", "定妆", "锚点", "种子", "音色克隆", "声音克隆", "数据模型",
         "表结构", "品牌规范", "视觉规范", "骨骼"],
    ),
    "L2b": (
        ["txt2img", "text to image", "flux", "midjourney", "sdxl", r"comfy ?ui", r"dall ?e",
         "ideogram", "kolors", "generate image", "image generation", "create image",
         "keyframe", "illustration", "tts", r"eleven ?labs", "suno", "udio",
         "generate music", "voiceover", "build component", "variant", "sql", "copywriting",
         "translate", "3d mesh", "blender"],
        ["文生图", "生图", "分镜图", "关键帧", "插画", "配音", "语音合成", "音效", "配乐",
         "文案", "翻译", "字幕", "组件", "模块", "图表", "建模"],
    ),
    "L3": (
        ["storyboard", "timeline", "sequencer", "assemble", "assembly", "integrate",
         "integration", "auto layout", "align", "alignment", "compose", "composition",
         "e2e", "playwright", "cypress", r"lip ?sync", "virtual try on", "video effect"],
        ["故事板", "故事版", "动态分镜", "时间轴", "排版", "组装", "对齐", "口型同步",
         "对口型", "联调", "集成", "测试", "校验"],
    ),
    "L4": (
        ["img2video", "image to video", "text to video", "txt2video", "generate video",
         "video generation", "sora", "runway", "kling", "luma", "pika", "hailuo", r"veo\d*",
         r"wan\s?2\w*", "seedance", "render", "ffmpeg", "transcode", "compile", "docker build",
         "pdf export"],
        ["图生视频", "文生视频", "视频生成", "视频合成", "渲染", "导出", "成片", "压制",
         "混音", "母带", "编译", "打包", "可灵", "即梦", "海螺"],
    ),
}
TIER_RE = {t: (_en(en), _zh(zh)) for t, (en, zh) in _TIER_TERMS.items()}
TIER_ORDER = ["L0-Ambient", "L1", "L2a", "L2b", "L3", "L4", "L5"]

# --------------------------------------------------------------------------- #
# Risk lexicon
# --------------------------------------------------------------------------- #
# Destructive: irreversible whatever the surrounding wording. Never downgraded.
_DESTRUCTIVE_EN = _en([
    r"drop\s+(?:table|database|db|schema|collection)",
    r"truncate\s+(?:table|database|db|data)",
    r"rm\s+(?:rf|fr|r)",
    r"push\s+(?:force|f)", r"force\s+push", r"reset\s+hard",
    r"delet(?:e|ing)\s+(?:all\s+|the\s+)?(?:prod|production|database|db|repo|repository|"
    r"bucket|account|user|branch|file|record|row|table|volume|cluster|secret|key)",
    r"destroy\s+(?:resource|cluster|infra|environment|instance|database|stack)",
    r"terraform\s+destroy", r"overwrit(?:e|ing|ten)",
    r"merge\s+(?:pull\s+request|pr)",
    r"(?:grant|revoke)\s+(?:access|permission|role|admin)",
    r"(?:change|modify|set|update)\s+permission",
    r"wipe\s+(?:data|disk|drive|device|database|db)",
    r"production\s+(?:environment|database|db|server|deploy|data|cluster|system)",
    r"prod\s+(?:env|environment|db|database|server|deploy|data|cluster)",
    r"live\s+(?:environment|site|database|system)",
])
_DESTRUCTIVE_ZH = _zh([
    "删库", "删除(?:文件|数据|分支|仓库|账号|账户|数据库|线上|生产)",
    "清空(?:数据|数据库|表)", "强制覆盖", "覆盖(?:原文件|线上|生产)", "强制推送",
    "重置(?:数据库|线上)", "修改权限",
])

# External: visible outside the workspace or spends real money. Weak: a clear
# read-only statement downgrades them.
_EXTERNAL_EN = _en([
    "publish", "deploy", "deployment", "send email", "send mail", "send message",
    "send sms", "send dm", "email blast", "mass email", "post social", "post to social",
    "tweet", "terraform", "kubectl apply", "payment", "stripe", "paypal",
    r"stripe\s+checkout", r"checkout\s+(?:session|flow)",
    r"charge\s+(?:card|customer|user|account)", r"create\s+charge", "refund", "purchase", "buy",
    "place order", r"transfer\s+(?:fund|money|asset|balance)", "wire transfer",
    r"(?:create|publish|cut)\s+release",
])
_EXTERNAL_ZH = _zh([
    "一键发布", "自动发布", "定时发布", "批量发布", "对外发布", "发布到",
    "发布(?:视频|文章|笔记|内容|帖子|动态|推文|文案)", "发布器", "发布工具", "群发",
    "推送给", "上线", "一键部署", "自动部署", "部署(?:到|上线|服务|应用|线上|生产|脚本|工具)",
    "发送邮件", "发邮件", "发送短信", "发短信",
    "(?:自动|一键|直接|执行|发起|确认)(?:支付|付款|转账|购买|下单)",
    "扣款", "退款", "转账", "下单", "收款", "支付(?:插件|接口|网关|工具|服务|请求|执行)",
    "生产环境",
])
_EXTERNAL_RAW_ZH = _zh(["支付宝(?:支付|转账|收款|付款)"])  # checked before neutral words are stripped

# Social platforms only count together with a publishing verb.
_PLATFORM_EN = _en(["youtube", "tiktok", "bilibili", "xiaohongshu", "vercel", "netlify"])
_PLATFORM_ZH = _zh(["小红书", "抖音", "快手", "公众号", "微博", "视频号"])
_PUBLISH_VERB_EN = _en(["publish", "post", "upload", "send", "submit", "schedule", "deploy"])
_PUBLISH_VERB_ZH = _zh(["发布", "发送", "群发", "推送", "投放", "上传", "发表"])

# Words that look risky but are ordinary nouns (press conference, online+offline, Alipay).
_ZH_NEUTRAL = ["发布会", "新闻发布", "线上线下", "支付宝", "购买力", "购买意向"]

_READONLY_EN = _en([r"read\s+only", "readonly", r"view\s+only", r"query\s+only",
                    r"no\s+writes?", r"without\s+writing", r"non\s+destructive"])
_READONLY_ZH = _zh(["只读", "仅查询", "仅读取", "只查询", "不修改", "不写入", "只用于查询"])

# Cost. STRONG terms are safe to apply to individual tool names; HINT terms
# (credit, balance, subscription ...) only make sense in prose descriptions.
_COST_STRONG_EN = _en([
    "paid", r"pay\s+per", "pay", "billing", "billed", r"price\s+per", r"paid\s+api",
    "sora", "runway", "kling", "hailuo", "seedance", "luma", "pika", "suno", "udio",
    "heygen", "synthesia", r"eleven ?labs", "midjourney", r"veo\d*",
    "generate video", "video generation", "text to video", "image to video",
    "txt2video", "img2video",
])
_COST_STRONG_ZH = _zh(["付费", "计费", "扣费", "充值", "算力", "视频生成", "图生视频",
                       "文生视频", "按秒", "按量", "可灵", "即梦", "海螺"])
_COST_HINT_EN = _en(["credit", "subscription", r"account\s+balance", r"credit\s+balance"])
_COST_HINT_ZH = _zh(["额度", "账户余额"])

_COMPOSITE_EN = _en([r"git ?hub", r"git ?lab", "supabase", "postgres", "notion", "figma",
                     "cloudflare", "mcp kling", "kie ai", r"comfy ?ui"])


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _risk_signals(raw: str, norm: str, *, names_only: bool = False) -> dict:
    """Return evidence lists for destructive / external / cost signals."""
    zh_text = raw
    for word in _ZH_NEUTRAL:
        zh_text = zh_text.replace(word, "")

    destructive = _find(_DESTRUCTIVE_EN, _DESTRUCTIVE_ZH, norm, zh_text)

    external = _find(_EXTERNAL_EN, _EXTERNAL_ZH, norm, zh_text)
    external += [m.group(0) for m in _EXTERNAL_RAW_ZH.finditer(raw)]
    if (_PLATFORM_EN.search(norm) and _PUBLISH_VERB_EN.search(norm)) or (
        _PLATFORM_ZH.search(raw) and _PUBLISH_VERB_ZH.search(raw)
    ):
        external.append("platform+publish")
    readonly = bool(_READONLY_EN.search(norm) or _READONLY_ZH.search(raw))
    if readonly:
        external = []

    cost = _find(_COST_STRONG_EN, _COST_STRONG_ZH, norm, zh_text)
    if not names_only:
        cost += _find(_COST_HINT_EN, _COST_HINT_ZH, norm, zh_text)
    return {"destructive": destructive, "external": external, "cost": cost, "read_only": readonly}


def classify_text(text: str) -> dict:
    """Classify a skill/MCP description into candidate tiers and a default gate."""
    norm = normalize(text)
    sig = _risk_signals(text, norm)
    risk = sig["destructive"] + sig["external"]

    tiers = [t for t in TIER_ORDER if t in TIER_RE and _find(*TIER_RE[t], norm, text)]
    if risk:
        tiers.append("L5")

    non_ambient = [t for t in tiers if t != "L0-Ambient"]
    is_composite = bool(_COMPOSITE_EN.search(norm)) or len(non_ambient) >= 2
    needs_probe = not tiers

    if risk:
        gate = "high_risk_confirm"
    elif sig["cost"]:
        gate = "cost_confirm"
    elif needs_probe:
        gate = "probe_required"
    else:
        gate = "auto"

    return {
        "candidate_tiers": tiers,
        "is_multi_tier_or_composite": is_composite,
        "default_gate": gate,
        "cost_sensitive": bool(sig["cost"]),
        "external_mutation_risk": bool(risk),
        "needs_probe": needs_probe,
        "read_only_declared": sig["read_only"],
        "evidence": {"risk": risk, "cost": sig["cost"]},
    }


def classify_tool_name(name: str) -> dict:
    """Strict, name-level check used by the pipeline validator.

    Only high-precision signals: destructive/external actions and paid-service names.
    """
    norm = normalize(name, split_camel=True)
    sig = _risk_signals(name, norm, names_only=True)
    return {
        "risk": sig["destructive"] + sig["external"],
        "cost": sig["cost"],
    }


# --------------------------------------------------------------------------- #
# SKILL.md front-matter (minimal YAML: plain, quoted and block scalars)
# --------------------------------------------------------------------------- #
_BLOCK_MARKERS = {">", ">-", ">+", "|", "|-", "|+"}


def parse_frontmatter(raw: str) -> dict[str, str] | None:
    raw = raw.lstrip("﻿").replace("\r\n", "\n")
    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", raw, re.DOTALL)
    if not m:
        return None
    lines = m.group(1).split("\n")
    out: dict[str, str] = {}
    i = 0
    while i < len(lines):
        km = re.match(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", lines[i])
        if not km:
            i += 1
            continue
        key, val = km.group(1), km.group(2).strip()
        i += 1
        cont: list[str] = []
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            cont.append(lines[i].strip())
            i += 1
        while cont and not cont[-1]:
            cont.pop()
        if val in _BLOCK_MARKERS:
            sep = "\n" if val.startswith("|") else " "
            val = sep.join(c for c in cont if c or sep == "\n")
        elif cont:
            val = " ".join([val] + [c for c in cont if c]).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def parse_skill_frontmatter(skill_md: Path) -> dict | None:
    try:
        raw = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    fm = parse_frontmatter(raw)
    if fm is None:
        return None
    name = fm.get("name") or skill_md.parent.name
    desc = fm.get("description", "")
    if name == "eddyy-skill-orchestrator":
        return None
    return {
        "name": name,
        "type": "skill",
        "path": str(skill_md),
        "description": desc[:240],
        **classify_text(f"{name} {desc}"),
    }


def scan_skills(roots: list[Path]) -> list[dict]:
    """Index SKILL.md files under the given roots.

    Deduplicates by skill name. Plugin caches keep several installed versions
    side by side, so among same-name candidates the newest SKILL.md wins
    (mtime, falling back to path order) and every loser is recorded so the
    caller can warn instead of silently shadowing a skill.
    """
    found: list[dict] = []
    candidates: dict[str, list[dict]] = {}
    order: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            info = parse_skill_frontmatter(skill_md)
            if not info:
                continue
            name = info["name"]
            try:
                info["_mtime"] = skill_md.stat().st_mtime
            except OSError:
                info["_mtime"] = 0.0
            if name not in candidates:
                candidates[name] = []
                order.append(name)
            candidates[name].append(info)
    collisions: list[dict] = []
    for name in order:
        group = candidates[name]
        group.sort(key=lambda d: d.get("_mtime", 0.0), reverse=True)  # newest first
        winner = group[0]
        for loser in group[1:]:
            collisions.append({"name": name, "kept": winner["path"], "shadowed": loser["path"]})
        winner.pop("_mtime", None)
        found.append(winner)
    scan_skills.last_collisions = collisions  # type: ignore[attr-defined]
    return found


scan_skills.last_collisions: list[dict] = []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# MCP configs (classification sees name + command + args + url host only)
# --------------------------------------------------------------------------- #
def _classifiable_text(name: str, cfg: object) -> str:
    """Build the text we classify. Deliberately excludes env, headers and any secret."""
    parts = [name]
    if isinstance(cfg, dict):
        command = cfg.get("command")
        if isinstance(command, str):
            parts.append(command)
        args = cfg.get("args")
        if isinstance(args, list):
            parts.extend(a for a in args if isinstance(a, str))
        url = cfg.get("url")
        if isinstance(url, str):
            host = urlparse(url).hostname
            if host:
                parts.append(host)
    return " ".join(parts)


def _server_dicts(data: dict) -> list[dict]:
    dicts = []
    top = data.get("mcpServers") or (data.get("mcp") or {}).get("servers")
    if isinstance(top, dict):
        dicts.append(top)
    codex_top = data.get("mcp_servers")
    if isinstance(codex_top, dict):
        dicts.append(codex_top)
    projects = data.get("projects")  # ~/.claude.json keeps per-project servers here
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
                dicts.append(proj["mcpServers"])
    return dicts


def scan_mcp_configs(config_paths: list[Path]) -> list[dict]:
    servers: list[dict] = []
    seen: set[str] = set()
    for cfg_path in config_paths:
        if not cfg_path.exists():
            continue
        try:
            raw = cfg_path.read_text(encoding="utf-8", errors="ignore")
            if cfg_path.suffix.lower() == ".toml":
                if tomllib is None:
                    continue
                data = tomllib.loads(raw)
            else:
                data = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for mcp_dict in _server_dicts(data):
            for srv_name, srv_cfg in mcp_dict.items():
                if not isinstance(srv_cfg, dict):
                    continue
                if srv_name in seen:
                    continue
                seen.add(srv_name)
                servers.append({
                    "name": srv_name,
                    "type": "mcp_server",
                    "source_config": str(cfg_path),
                    **classify_text(_classifiable_text(srv_name, srv_cfg)),
                })
    return servers


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def default_skill_roots(home: Path, cwd: Path) -> list[Path]:
    codex_skills = (
        Path(os.environ["CODEX_HOME"]) / "skills"
        if os.environ.get("CODEX_HOME")
        else home / ".codex" / "skills"
    )
    roots = [
        cwd / "skills", cwd / ".claude" / "skills", cwd / ".agents" / "skills",
        cwd / ".workbuddy" / "skills",
        home / ".claude" / "skills", home / ".agents" / "skills", home / ".workbuddy" / "skills",
        codex_skills,
        # Host plugin caches: built-in session skills often live under a
        # versioned cache directory instead of ~/.workbuddy/skills.
        home / ".workbuddy" / "plugins" / "cache",
        home / ".cursor" / "extensions",
    ]
    extra = os.environ.get("L0_SKILL_ROOTS", "")
    roots += [Path(p) for p in extra.split(os.pathsep) if p]
    return roots


def default_mcp_configs(home: Path, cwd: Path) -> list[Path]:
    codex_cfg = (
        Path(os.environ["CODEX_HOME"]) / "config.toml"
        if os.environ.get("CODEX_HOME")
        else home / ".codex" / "config.toml"
    )
    return [
        cwd / ".mcp.json", cwd / ".cursor" / "mcp.json", cwd / ".codex" / "config.toml",
        home / ".claude.json", home / ".cursor" / "mcp.json", codex_cfg,
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Advisory L0 inventory scanner")
    ap.add_argument("out", nargs="?", help="write the inventory JSON here instead of stdout")
    ap.add_argument("--classify", metavar="TEXT",
                    help="classify one tool/skill name+description and print the result")
    args = ap.parse_args(argv)

    if args.classify is not None:
        print(json.dumps(classify_text(args.classify), indent=2, ensure_ascii=False))
        return 0

    home, cwd = Path(os.path.expanduser("~")), Path.cwd()
    roots = default_skill_roots(home, cwd)
    skills = scan_skills(roots)
    mcps = scan_mcp_configs(default_mcp_configs(home, cwd))
    payload = {
        "Note": ("Advisory heuristics over names/descriptions only. Tools with "
                 "default_gate=probe_required were not recognised: inspect their schema; "
                 "if that is impossible treat them as high_risk_confirm. The host's own "
                 "permission prompts always take precedence."),
        "total_skills": len(skills),
        "total_mcp_servers": len(mcps),
        "inventory": skills + mcps,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[L0-Scanner] Indexed {len(skills)} skills and {len(mcps)} MCP servers -> {args.out}")
    else:
        print(text)
    _print_scan_summary(roots, skills, mcps)
    return 0


def _print_scan_summary(roots: list[Path], skills: list[dict], mcps: list[dict]) -> None:
    """Stderr summary: roots that matched nothing, name collisions, probe count."""
    hits = {s.get("path", "") for s in skills}
    missing = [str(r) for r in roots
               if r.exists() and not any(str(r) in p for p in hits)]
    absent = [str(r) for r in roots if not r.exists()]
    probes = [s.get("name", "?") for s in skills + mcps if s.get("needs_probe")]
    if absent:
        print(f"[L0-Scanner] roots not present (skipped): {', '.join(absent)}", file=sys.stderr)
    if missing:
        print(f"[L0-Scanner] roots present but no SKILL.md found: {', '.join(missing)}", file=sys.stderr)
    for c in getattr(scan_skills, "last_collisions", []):
        print(f"[L0-Scanner] duplicate name '{c['name']}': kept {c['kept']}, "
              f"shadowed {c['shadowed']}", file=sys.stderr)
    if probes:
        print(f"[L0-Scanner] {len(probes)} unrecognised tool(s) -> probe_required: "
              f"{', '.join(probes[:20])}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
