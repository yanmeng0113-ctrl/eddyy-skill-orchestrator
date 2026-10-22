#!/usr/bin/env python3
"""Persistent checkpoint for L0 pipelines (stdlib only, Python >= 3.9).

Keeps locked anchors (seeds, reference URIs, schema paths), completed tiers, retry
counters and gate confirmations across circuit-breaker pauses and context compaction.

Safety properties
  * A corrupt or unreadable state file is NEVER silently reset: the command fails
    (exit 4) and a copy of the original is kept next to it.
  * Writes are atomic (temp file + os.replace); a crash cannot leave a half-written file.
  * `init` refuses to overwrite a pipeline that is still in progress unless --force.
  * Tier names are validated; a locked anchor is not overwritten without --force.
  * `rollback` counts retries per tier and stops at the limit (exit 3) so a faulty node
    cannot loop forever: escalate to the user instead.

State file: --file, else $L0_STATE_FILE, else ./.l0-state.json. Use one file per
project/pipeline (e.g. --file .l0-state.trailer.json) when several run side by side.

Exit codes: 0 ok, 1 error, 2 usage, 3 retry limit reached, 4 state file corrupt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_FILE = ".l0-state.json"
SCHEMA_VERSION = 3
TIER_RE = re.compile(r"^L(?:1|2a|2b|3|4|5)(?:\.\d+[ab]?)*$")  # nested ids like L3.2a allowed
BASE_ORDER = {"L1": 1, "L2a": 2, "L2b": 3, "L3": 4, "L4": 5, "L5": 6}
STATUSES = ("in_progress", "awaiting_confirmation", "completed", "rolled_back", "escalated")
NODE_STATUSES = ("pending", "in_progress", "completed", "failed", "needs_rerun")
ACTIVE = ("in_progress", "awaiting_confirmation", "rolled_back")

EXIT_ERROR, EXIT_RETRY, EXIT_CORRUPT = 1, 3, 4


class StateError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _base(tier: str) -> int:
    return BASE_ORDER[tier.split(".")[0]]


def _check_tier(tier: str) -> str:
    if not TIER_RE.match(tier):
        raise StateError(f"invalid tier '{tier}': expected L1, L2a, L2b, L3, L4, L5 "
                         "(nested ids such as L3.2a are allowed)")
    return tier


def _backup(path: Path, label: str) -> Path:
    """Copy `path` next to itself; reuse an existing identical backup instead of piling up copies."""
    data = path.read_bytes()
    for existing in sorted(path.parent.glob(f"{path.name}.{label}-*")):
        try:
            if existing.read_bytes() == data:
                return existing
        except OSError:
            continue
    target = path.with_name(f"{path.name}.{label}-{_stamp()}")
    shutil.copy2(path, target)
    return target


def load_state(path: Path) -> dict:
    if not path.exists():
        raise StateError(f"state file not found: {path} (run `init` first)")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("locked_anchors", {}), dict) \
                or not isinstance(state.get("completed_tiers", {}), dict):
            raise ValueError("unexpected structure")
    except (OSError, ValueError) as exc:
        try:
            kept = _backup(path, "corrupt")
            where = f"A copy was kept at {kept}."
        except OSError:
            where = "The original file was left untouched."
        raise StateError(
            f"state file {path} is unreadable ({exc}). {where} Nothing was changed. "
            "Repair or remove the file, or run `init --force` to start over.",
            EXIT_CORRUPT,
        )
    state.setdefault("schema_version", 1)
    state.setdefault("scope", "")
    state.setdefault("entry_tier", "L1")
    state.setdefault("current_tier", state["entry_tier"])
    state.setdefault("status", "in_progress")
    state.setdefault("locked_anchors", {})
    state.setdefault("completed_tiers", {})
    state.setdefault("retries", {})
    state.setdefault("max_retries", 2)
    state.setdefault("confirmations", [])
    state.setdefault("nodes", {})
    state.setdefault("approved_budget_usd", 0.0)
    state.setdefault("spent_usd", 0.0)

    # Migrate old tier-based files (schema_version 1/2 with completed_tiers):
    # node id = tier name, status completed, artifacts=[output], depends_on = previous tier by order
    if state["completed_tiers"]:
        sorted_tiers = sorted(
            state["completed_tiers"].keys(),
            key=lambda t: (BASE_ORDER.get(t.split(".")[0], 99), t),
        )
        prev_t = None
        for t in sorted_tiers:
            t_info = state["completed_tiers"][t]
            output = t_info.get("output") if isinstance(t_info, dict) else None
            artifacts = [output] if output else []
            depends_on = [prev_t] if prev_t else []
            if t not in state["nodes"]:
                state["nodes"][t] = {
                    "status": "completed",
                    "artifacts": artifacts,
                    "depends_on": depends_on,
                    "tool": "",
                    "fallback": [],
                }
            else:
                node = state["nodes"][t]
                if node.get("status") == "completed" and not node.get("artifacts") and artifacts:
                    node["artifacts"] = artifacts
            prev_t = t

    return state


def save_state(path: Path, state: dict) -> None:
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ commands
def cmd_init(path: Path, args: argparse.Namespace) -> None:
    _check_tier(args.entry)
    if path.exists():
        try:
            existing = load_state(path)
            active = existing.get("status") in ACTIVE
        except StateError:
            active = True  # corrupt file: treat as precious
        if active and not args.force:
            raise StateError(f"{path} holds a pipeline that is still in progress. Use a different "
                             "--file, finish it, or pass --force (the old file is backed up).")
        if args.force:
            _backup(path, "bak")
    state = {
        "scope": args.scope, "entry_tier": args.entry, "current_tier": args.entry,
        "status": "in_progress", "locked_anchors": {}, "completed_tiers": {},
        "retries": {}, "max_retries": args.max_retries, "confirmations": [],
        "nodes": {}, "approved_budget_usd": 0.0, "spent_usd": 0.0,
    }
    save_state(path, state)
    print(f"[L0-State] Initialized pipeline '{args.scope}' at {args.entry} -> {path}")


def cmd_lock(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    anchors = state["locked_anchors"]
    if args.key in anchors and anchors[args.key] != args.value and not args.force:
        raise StateError(f"anchor '{args.key}' is already locked to '{anchors[args.key]}'. "
                         "Pass --force only if you deliberately re-generate it.")
    anchors[args.key] = args.value
    save_state(path, state)
    print(f"[L0-State] Locked anchor '{args.key}' = '{args.value}'")


def cmd_complete(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    tier = _check_tier(args.tier)
    if args.next_tier:
        _check_tier(args.next_tier)
    state["completed_tiers"][tier] = {"output": args.output, "timestamp": _now()}
    if args.next_tier:
        state["current_tier"] = args.next_tier
    state["status"] = args.status
    sorted_tiers = sorted(
        state["completed_tiers"].keys(),
        key=lambda t: (BASE_ORDER.get(t.split(".")[0], 99), t),
    )
    idx = sorted_tiers.index(tier)
    prev_tier = sorted_tiers[idx - 1] if idx > 0 else None
    if tier not in state["nodes"]:
        state["nodes"][tier] = {
            "status": "completed",
            "artifacts": [args.output] if args.output else [],
            "depends_on": [prev_tier] if prev_tier else [],
            "tool": "",
            "fallback": [],
        }
    else:
        node = state["nodes"][tier]
        node["status"] = "completed"
        if args.output and args.output not in node["artifacts"]:
            node["artifacts"].append(args.output)
        if prev_tier and prev_tier not in node["depends_on"]:
            node["depends_on"].append(prev_tier)
    save_state(path, state)
    print(f"[L0-State] Tier {tier} marked complete. Current tier: {state['current_tier']} ({state['status']})")


def cmd_confirm(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    tier = _check_tier(args.tier)
    if state["status"] != "awaiting_confirmation":
        raise StateError(f"nothing to confirm: status is '{state['status']}', not 'awaiting_confirmation'.")
    if tier != state["current_tier"]:
        raise StateError(f"the pipeline is waiting at {state['current_tier']}, not {tier}.")
    state["confirmations"].append({"tier": tier, "note": args.note or "", "timestamp": _now()})
    state["status"] = "in_progress"
    save_state(path, state)
    print(f"[L0-State] Gate before {tier} confirmed by the user. Status: in_progress")


def cmd_rollback(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    target = _check_tier(args.to)
    used = state["retries"].get(target, 0)
    if used >= state["max_retries"]:
        state["status"] = "escalated"
        save_state(path, state)
        raise StateError(f"retry limit reached for {target} ({used}/{state['max_retries']}). "
                         "Stop and escalate to the user with what failed and what was tried.", EXIT_RETRY)
    state["retries"][target] = used + 1
    for done in list(state["completed_tiers"]):
        if _base(done) >= _base(target):
            del state["completed_tiers"][done]
    state["current_tier"] = target
    state["status"] = "rolled_back"
    state["last_rollback"] = {"to": target, "reason": args.reason or "", "timestamp": _now()}
    save_state(path, state)
    msg = f"[L0-State] Rolled back to {target} (retry {used + 1}/{state['max_retries']})."
    if _base(target) <= BASE_ORDER["L2a"] and state["locked_anchors"]:
        msg += " Anchors may be stale; re-lock regenerated ones with `lock --force`."
    print(msg)


def cmd_show(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    if not args.brief:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return
    done = ", ".join(state["completed_tiers"]) or "none"
    anchors = ", ".join(f"{k}={v}" for k, v in state["locked_anchors"].items()) or "none"
    print(f"Scope: {state['scope']}")
    print(f"Position: {state['current_tier']} ({state['status']})")
    print(f"Completed: {done}")
    print(f"Locked anchors: {anchors}")
    print(f"Retries: {state['retries'] or 'none'} (limit {state['max_retries']} per tier)")
    if state.get("nodes"):
        nodes_summary = ", ".join(f"{k}:{v.get('status')}" for k, v in state["nodes"].items())
        print(f"Nodes: {nodes_summary}")
    if state["status"] == "awaiting_confirmation":
        print(f"NEXT: ask the user for go/no-go before {state['current_tier']}, then run `confirm`.")
    elif state["status"] == "escalated":
        print("NEXT: retry limit hit. Report to the user; do not continue automatically.")
    elif state["status"] == "completed":
        print("NEXT: nothing, the pipeline is complete.")
    else:
        print(f"NEXT: continue at {state['current_tier']}.")


def cmd_node_add(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    node_id = args.id.strip()
    if not node_id:
        raise StateError("node id cannot be empty")
    deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
    tool = args.tool.strip()
    fallback = [f.strip() for f in args.fallback.split(",") if f.strip()]
    if node_id not in state["nodes"]:
        state["nodes"][node_id] = {
            "status": "pending",
            "artifacts": [],
            "depends_on": deps,
            "tool": tool,
            "fallback": fallback,
        }
    else:
        node = state["nodes"][node_id]
        node["depends_on"] = deps
        node["tool"] = tool
        node["fallback"] = fallback
    save_state(path, state)
    print(f"[L0-State] Node '{node_id}' added (status: {state['nodes'][node_id]['status']}).")


def cmd_node_complete(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    node_id = args.id.strip()
    if node_id not in state["nodes"]:
        state["nodes"][node_id] = {
            "status": "pending",
            "artifacts": [],
            "depends_on": [],
            "tool": "",
            "fallback": [],
        }
    node = state["nodes"][node_id]
    node["status"] = "completed"
    for art in args.artifact:
        for p in art.split(","):
            p = p.strip()
            if p and p not in node["artifacts"]:
                node["artifacts"].append(p)
    save_state(path, state)
    print(f"[L0-State] Node '{node_id}' completed. Artifacts: {node['artifacts']}")


def cmd_node_fail(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    node_id = args.id.strip()
    if node_id not in state["nodes"]:
        state["nodes"][node_id] = {
            "status": "pending",
            "artifacts": [],
            "depends_on": [],
            "tool": "",
            "fallback": [],
        }
    state["nodes"][node_id]["status"] = "failed"
    save_state(path, state)
    print(f"[L0-State] Node '{node_id}' marked failed.")


def cmd_rerun(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    node_id = args.id.strip()
    if node_id not in state["nodes"]:
        raise StateError(f"node '{node_id}' not found in state")

    used = state["retries"].get(node_id, 0)
    if used >= state["max_retries"]:
        state["status"] = "escalated"
        save_state(path, state)
        raise StateError(
            f"retry limit reached for node '{node_id}' ({used}/{state['max_retries']}). "
            "Stop and escalate to the user with what failed and what was tried.",
            EXIT_RETRY,
        )
    state["retries"][node_id] = used + 1

    downstream = {node_id}
    changed = True
    while changed:
        changed = False
        for nid, ninfo in state["nodes"].items():
            if nid not in downstream:
                if any(d in downstream for d in ninfo.get("depends_on", [])):
                    downstream.add(nid)
                    changed = True

    for nid in downstream:
        state["nodes"][nid]["status"] = "needs_rerun"
        state["nodes"][nid]["artifacts"] = []

    state["status"] = "rolled_back"
    state["last_rerun"] = {
        "id": node_id,
        "reason": args.reason or "",
        "timestamp": _now(),
        "downstream": sorted(downstream),
    }
    save_state(path, state)
    print(
        f"[L0-State] Marked node '{node_id}' and downstream nodes ({', '.join(sorted(downstream))}) as needs_rerun "
        f"(retry {used + 1}/{state['max_retries']})."
    )


def cmd_next_tool(path: Path, args: argparse.Namespace) -> None:
    state = load_state(path)
    node_id = args.id.strip()
    if node_id not in state["nodes"]:
        raise StateError(f"node '{node_id}' not found in state", EXIT_ERROR)
    node = state["nodes"][node_id]
    unavail: set[str] = set()
    for item in args.unavailable:
        for t in item.split(","):
            if t.strip():
                unavail.add(t.strip())
    candidates: list[str] = []
    if node.get("tool"):
        candidates.append(node["tool"])
    for fb in node.get("fallback", []):
        if fb not in candidates:
            candidates.append(fb)
    for cand in candidates:
        if cand not in unavail:
            print(cand)
            return
    raise StateError(
        f"no available tool for node '{node_id}' (candidates: {candidates}, unavailable: {sorted(unavail)})",
        EXIT_ERROR,
    )


def cmd_budget(path: Path, args: argparse.Namespace) -> None:
    state = None
    if path.exists():
        try:
            state = load_state(path)
        except StateError:
            pass

    approved = args.approved
    if approved is None:
        if state is not None:
            approved = float(state.get("approved_budget_usd") or (state.get("budget") or {}).get("approved", 0.0))
        else:
            approved = 0.0

    spent = args.spent
    if spent is None:
        if state is not None:
            spent = float(state.get("spent_usd") or (state.get("budget") or {}).get("spent", 0.0))
        else:
            spent = 0.0

    next_cost = float(args.next_cost if args.next_cost is not None else 0.0)
    total = spent + next_cost

    if total > approved:
        raise StateError(
            f"stop and confirm: spent (${spent:.2f}) + next cost (${next_cost:.2f}) = ${total:.2f} "
            f"exceeds approved budget (${approved:.2f})",
            EXIT_RETRY,
        )

    if state is not None:
        state["approved_budget_usd"] = approved
        state["spent_usd"] = spent
        state.setdefault("budget", {})["approved"] = approved
        state["budget"]["spent"] = spent
        save_state(path, state)

    print(f"[L0-State] Budget ok: spent ${spent:.2f} + next ${next_cost:.2f} = ${total:.2f} <= approved ${approved:.2f}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="L0 pipeline checkpoint manager")
    p.add_argument("--file", default=os.environ.get("L0_STATE_FILE", DEFAULT_STATE_FILE),
                   help="state file (default: $L0_STATE_FILE or ./.l0-state.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="start a new pipeline state")
    s.add_argument("--scope", required=True)
    s.add_argument("--entry", default="L1")
    s.add_argument("--max-retries", type=int, default=2, choices=range(0, 6), metavar="0-5")
    s.add_argument("--force", action="store_true", help="replace an in-progress state (old one is backed up)")

    s = sub.add_parser("lock", help="lock an L1/L2a anchor (seed, reference URI, schema path)")
    s.add_argument("--key", required=True)
    s.add_argument("--value", required=True)
    s.add_argument("--force", action="store_true", help="overwrite an existing, different value")

    s = sub.add_parser("complete-tier", help="record completion of a tier")
    s.add_argument("--tier", required=True)
    s.add_argument("--output", required=True, help="summary or path of the tier output")
    s.add_argument("--next-tier", default="")
    s.add_argument("--status", default="in_progress", choices=STATUSES)

    s = sub.add_parser("confirm", help="record that the user approved the gate before a tier")
    s.add_argument("--tier", required=True)
    s.add_argument("--note", default="", help="what was approved, e.g. 'USD 18 for Kling render'")

    s = sub.add_parser("rollback", help="re-run from an upstream tier (counts retries, enforces the limit)")
    s.add_argument("--to", required=True)
    s.add_argument("--reason", default="")

    s = sub.add_parser("show", help="print the state (or a resume summary with --brief)")
    s.add_argument("--brief", action="store_true")

    s = sub.add_parser("node-add", help="add a DAG node to the state")
    s.add_argument("--id", required=True, help="unique node identifier")
    s.add_argument("--depends-on", default="", help="comma-separated dependency node ids")
    s.add_argument("--tool", default="", help="primary tool for this node")
    s.add_argument("--fallback", default="", help="comma-separated fallback tools")

    s = sub.add_parser("node-complete", help="mark a node completed and record its artifacts")
    s.add_argument("--id", required=True, help="node identifier")
    s.add_argument("--artifact", action="append", default=[], help="artifact path produced by node (repeatable)")

    s = sub.add_parser("node-fail", help="mark a node failed")
    s.add_argument("--id", required=True, help="node identifier")

    s = sub.add_parser("rerun", help="re-run node and all transitive downstream nodes")
    s.add_argument("--id", required=True, help="node identifier")
    s.add_argument("--reason", default="", help="reason for rerun")

    s = sub.add_parser("next-tool", help="print the first available tool from primary + fallbacks")
    s.add_argument("--id", required=True, help="node identifier")
    s.add_argument("--unavailable", action="append", default=[], help="comma-separated unavailable tools (repeatable)")

    s = sub.add_parser("budget", help="budget check against approved spend")
    s.add_argument("--approved", type=float, default=None, help="approved budget in USD")
    s.add_argument("--spent", type=float, default=None, help="already spent in USD")
    s.add_argument("--next-cost", type=float, default=0.0, help="next estimated cost in USD")

    return p


HANDLERS = {
    "init": cmd_init,
    "lock": cmd_lock,
    "complete-tier": cmd_complete,
    "confirm": cmd_confirm,
    "rollback": cmd_rollback,
    "show": cmd_show,
    "node-add": cmd_node_add,
    "node-complete": cmd_node_complete,
    "node-fail": cmd_node_fail,
    "rerun": cmd_rerun,
    "next-tool": cmd_next_tool,
    "budget": cmd_budget,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        HANDLERS[args.cmd](Path(args.file), args)
    except StateError as exc:
        print(f"[L0-State] ERROR: {exc}", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
