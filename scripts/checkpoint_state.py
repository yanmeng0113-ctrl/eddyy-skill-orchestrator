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
import math
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


def _tier_sort_key(tier: str) -> tuple[int, int, tuple[tuple[int, int], ...], str]:
    """Sort stage ids by their declared top-level order and numeric nested parts."""
    base, *nested = tier.split(".")
    base_order = BASE_ORDER.get(base, 99)
    parts: list[tuple[int, int]] = []
    for part in nested:
        match = re.fullmatch(r"(\d+)([ab]?)", part)
        if not match:
            return (base_order, 1, (), tier)
        suffix_order = {"": 0, "a": 1, "b": 2}[match.group(2)]
        parts.append((int(match.group(1)), suffix_order))
    return (base_order, 0, tuple(parts), tier)


def _is_tier_id(value: object) -> bool:
    return isinstance(value, str) and TIER_RE.fullmatch(value) is not None


def _downstream_nodes(nodes: dict, seeds: set[str]) -> set[str]:
    downstream = set(seeds)
    changed = True
    while changed:
        changed = False
        for node_id, info in nodes.items():
            if node_id not in downstream and any(dep in downstream for dep in info.get("depends_on", [])):
                downstream.add(node_id)
                changed = True
    return downstream


def _record_tier_completion(state: dict, tier: str, artifacts: list[str]) -> None:
    state["completed_tiers"][tier] = {
        "output": artifacts[-1] if artifacts else "",
        "timestamp": _now(),
    }


def _refresh_recovery_position(state: dict, status_after: str = "in_progress") -> None:
    recovery = state.get("recovery")
    if not isinstance(recovery, dict):
        return
    nodes = state["nodes"]
    affected = [
        node_id for node_id in recovery.get("nodes", [])
        if node_id in nodes and nodes[node_id].get("status") == "needs_rerun"
    ]
    ready = [
        node_id for node_id in affected
        if all(nodes.get(dep, {}).get("status") == "completed" for dep in nodes[node_id].get("depends_on", []))
    ]
    if not affected:
        state["current_tier"] = recovery.get("resume_tier", state["current_tier"])
        state.pop("current_node", None)
        state["status"] = status_after
        state.pop("recovery", None)
        return
    if ready:
        ready.sort(key=lambda node_id: (
            0 if _is_tier_id(nodes[node_id].get("tier_id")) else 1,
            _tier_sort_key(nodes[node_id]["tier_id"]) if _is_tier_id(nodes[node_id].get("tier_id")) else (99, 1, (), node_id),
            node_id,
        ))
        next_node = ready[0]
        tier_id = nodes[next_node].get("tier_id")
        if _is_tier_id(tier_id):
            state["current_tier"] = tier_id
            state.pop("current_node", None)
        else:
            state["current_node"] = next_node
    else:
        next_node = sorted(affected)[0]
        tier_id = nodes[next_node].get("tier_id")
        if _is_tier_id(tier_id):
            state["current_tier"] = tier_id
            state.pop("current_node", None)
        else:
            state["current_node"] = next_node
    state["status"] = status_after


def _validated_money(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise StateError(f"{label} must be a finite, non-negative number")
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        raise StateError(f"{label} must be a finite, non-negative number") from None
    if not math.isfinite(amount) or amount < 0:
        raise StateError(f"{label} must be a finite, non-negative number")
    return amount


def _state_budget_value(state: dict, current_key: str, legacy_key: str, label: str) -> float:
    if current_key in state:
        value = state[current_key]
    else:
        legacy = state.get("budget")
        if not isinstance(legacy, dict) or legacy_key not in legacy:
            raise StateError(f"{label} is missing from state; supply it explicitly")
        value = legacy[legacy_key]
    return _validated_money(value, label)


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
                or not isinstance(state.get("completed_tiers", {}), dict) \
                or not isinstance(state.get("nodes", {}), dict) \
                or any(not isinstance(node, dict) for node in state.get("nodes", {}).values()):
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

    # Migrate explicit completed-stage references. A stale reference paired with
    # a non-completed node is removed, never used to revive that node.
    if state["completed_tiers"]:
        sorted_tiers = sorted(
            state["completed_tiers"].keys(),
            key=_tier_sort_key,
        )
        prev_t = None
        for t in sorted_tiers:
            if not _is_tier_id(t):
                continue
            t_info = state["completed_tiers"][t]
            output = t_info.get("output") if isinstance(t_info, dict) else None
            artifacts = [output] if output else []
            depends_on = [prev_t] if prev_t else []
            if t not in state["nodes"]:
                state["nodes"][t] = {
                    "tier_id": t,
                    "status": "completed",
                    "artifacts": artifacts,
                    "depends_on": depends_on,
                    "tool": "",
                    "fallback": [],
                }
            else:
                node = state["nodes"][t]
                if not isinstance(node, dict):
                    raise StateError(f"state file {path} has an invalid node record for {t}", EXIT_CORRUPT)
                node.setdefault("tier_id", t)
                if node.get("status") == "completed" and not node.get("artifacts") and artifacts:
                    node["artifacts"] = artifacts
                elif node.get("status") != "completed":
                    state["completed_tiers"].pop(t, None)
            if t in state["completed_tiers"]:
                prev_t = t

    # Keep stage references aligned with explicit tier metadata and node status.
    for node_id, node in state["nodes"].items():
        if not isinstance(node, dict):
            raise StateError(f"state file {path} has an invalid node record for {node_id}", EXIT_CORRUPT)
        tier_id = node.get("tier_id")
        if not _is_tier_id(tier_id):
            continue
        if node.get("status") == "completed":
            artifacts = node.get("artifacts", [])
            state["completed_tiers"].setdefault(
                tier_id,
                {"output": artifacts[-1] if artifacts else "", "timestamp": _now()},
            )
        else:
            state["completed_tiers"].pop(tier_id, None)

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
        "nodes": {},
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
        (t for t in state["completed_tiers"] if _is_tier_id(t)),
        key=_tier_sort_key,
    )
    idx = sorted_tiers.index(tier)
    prev_tier = sorted_tiers[idx - 1] if idx > 0 else None
    if tier not in state["nodes"]:
        state["nodes"][tier] = {
            "tier_id": tier,
            "status": "completed",
            "artifacts": [args.output] if args.output else [],
            "depends_on": [prev_tier] if prev_tier else [],
            "tool": "",
            "fallback": [],
        }
    else:
        node = state["nodes"][tier]
        node["tier_id"] = tier
        node["status"] = "completed"
        node.setdefault("artifacts", [])
        if args.output and args.output not in node["artifacts"]:
            node["artifacts"].append(args.output)
        node.setdefault("depends_on", [])
        if prev_tier and prev_tier not in node["depends_on"]:
            node["depends_on"].append(prev_tier)
    _record_tier_completion(state, tier, state["nodes"][tier].get("artifacts", []))
    if state.get("recovery"):
        _refresh_recovery_position(state, status_after=args.status)
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
    resume_tier = state["current_tier"]
    target_key = _tier_sort_key(target)
    stage_tiers = {
        tier for tier in state["completed_tiers"]
        if _is_tier_id(tier) and _tier_sort_key(tier) >= target_key
    }
    stage_tiers.update(
        node.get("tier_id") for node in state["nodes"].values()
        if _is_tier_id(node.get("tier_id")) and _tier_sort_key(node["tier_id"]) >= target_key
    )
    seeds = {
        node_id for node_id, node in state["nodes"].items()
        if node.get("tier_id") in stage_tiers
    }
    affected = _downstream_nodes(state["nodes"], seeds)
    for node_id in affected:
        node = state["nodes"][node_id]
        if node_id in seeds or node.get("status") in ("completed", "in_progress", "failed", "needs_rerun"):
            node["status"] = "needs_rerun"
        node["artifacts"] = []
    for done in list(state["completed_tiers"]):
        if (_is_tier_id(done) and _tier_sort_key(done) >= target_key) or any(
            state["nodes"].get(node_id, {}).get("tier_id") == done for node_id in affected
        ):
            del state["completed_tiers"][done]
    state["current_tier"] = target
    state.pop("current_node", None)
    state["status"] = "rolled_back"
    state["last_rollback"] = {
        "to": target, "reason": args.reason or "", "timestamp": _now(),
        "invalidated_nodes": sorted(affected), "invalidated_tiers": sorted(stage_tiers, key=_tier_sort_key),
    }
    state["recovery"] = {
        "kind": "stage", "target": target, "nodes": sorted(
            node_id for node_id in affected if state["nodes"][node_id].get("status") == "needs_rerun"
        ), "resume_tier": resume_tier,
    }
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
    done = ", ".join(sorted(state["completed_tiers"], key=_tier_sort_key)) or "none"
    position = state.get("current_node") or state["current_tier"]
    anchors = ", ".join(f"{k}={v}" for k, v in state["locked_anchors"].items()) or "none"
    print(f"Scope: {state['scope']}")
    print(f"Position: {position} ({state['status']})")
    print(f"Completed: {done}")
    print(f"Locked anchors: {anchors}")
    print(f"Retries: {state['retries'] or 'none'} (limit {state['max_retries']} per tier)")
    if state.get("nodes"):
        nodes_summary = ", ".join(f"{k}:{v.get('status')}" for k, v in state["nodes"].items())
        print(f"Nodes: {nodes_summary}")
    if state.get("current_node"):
        print(f"NEXT: re-run node {state['current_node']}.")
    elif state["status"] == "awaiting_confirmation":
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
    node.setdefault("artifacts", [])
    for art in args.artifact:
        for p in art.split(","):
            p = p.strip()
            if p and p not in node["artifacts"]:
                node["artifacts"].append(p)
    tier_id = node.get("tier_id")
    if _is_tier_id(tier_id):
        _record_tier_completion(state, tier_id, node["artifacts"])
    if state.get("recovery") and node_id in state["recovery"].get("nodes", []):
        _refresh_recovery_position(state)
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

    downstream = _downstream_nodes(state["nodes"], {node_id})

    for nid in downstream:
        state["nodes"][nid]["status"] = "needs_rerun"
        state["nodes"][nid]["artifacts"] = []

    for tier in list(state["completed_tiers"]):
        if any(state["nodes"].get(nid, {}).get("tier_id") == tier for nid in downstream):
            del state["completed_tiers"][tier]

    resume_tier = state["current_tier"]
    node_tier = state["nodes"][node_id].get("tier_id")
    state.pop("current_node", None)
    if _is_tier_id(node_tier):
        state["current_tier"] = node_tier
    else:
        state["current_node"] = node_id
    state["status"] = "rolled_back"
    affected_tiers = sorted(
        {state["nodes"][nid].get("tier_id") for nid in downstream if _is_tier_id(state["nodes"][nid].get("tier_id"))},
        key=_tier_sort_key,
    )
    state["last_rerun"] = {
        "id": node_id,
        "reason": args.reason or "",
        "timestamp": _now(),
        "downstream": sorted(downstream),
        "invalidated_tiers": affected_tiers,
    }
    state["recovery"] = {
        "kind": "node", "target": node_id, "nodes": sorted(downstream),
        "resume_tier": resume_tier,
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
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise StateError(f"state file {path} is inaccessible ({exc})", EXIT_CORRUPT) from None
    else:
        state = load_state(path)

    if args.approved is not None:
        approved = _validated_money(args.approved, "approved budget")
    elif state is not None:
        approved = _state_budget_value(state, "approved_budget_usd", "approved", "approved budget")
    else:
        raise StateError("approved budget is unknown; supply --approved or configure it in state")

    if args.spent is not None:
        spent = _validated_money(args.spent, "historical spend")
    elif state is not None:
        spent = _state_budget_value(state, "spent_usd", "spent", "historical spend")
    else:
        raise StateError("historical spend is unknown; supply --spent or record it in state")

    next_cost = _validated_money(args.next_cost if args.next_cost is not None else 0.0, "next cost")
    total = spent + next_cost
    if not math.isfinite(total):
        raise StateError("historical spend plus next cost is not finite; stop and confirm")

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
