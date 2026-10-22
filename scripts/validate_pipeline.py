#!/usr/bin/env python3
"""Deterministic validator for L0 pipeline plans (stdlib only, Python >= 3.9).

Exit codes: 0 = valid, 1 = plan has problems, 2 = usage / unreadable input.

Checks
  structure   required fields match assets/pipeline-schema.json (a test enforces this),
              correct types, no duplicate tier ids, tiers listed in tier order
  graph       depends_on / rollback_target must be strictly upstream AND present in the
              plan (or in satisfied_tiers); tiers before entry_tier must be satisfied
  L2 lock     L2b must depend on L2a whenever an L2a exists (planned or satisfied)
  gates       L5 is never 'auto'; a tool whose NAME signals an external or destructive
              action (stripe_payment, deploy_prod, merge_pull_request ...) needs
              'high_risk_confirm' on any tier; paid work under 'auto' must carry an
              estimate that fits inside approved_budget_usd; 'cost_confirm' needs an
              estimate; sub-pipeline gates and costs bubble up to the parent tier
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_inventory import classify_tool_name  # noqa: E402

TIER_ORDER = {"L1": 1, "L2a": 2, "L2b": 3, "L3": 4, "L4": 5, "L5": 6}
GATE_SEVERITY = {"auto": 0, "cost_confirm": 1, "high_risk_confirm": 2}
EXECUTION_MODES = {"sequential", "parallel"}
REQUIRED_TOP = ("scope", "entry_tier", "tiers")
REQUIRED_TIER = ("tier_id", "role", "tools", "execution_mode", "gate", "expected_output")
EPS = 1e-9


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _tier_list(value: object, name: str, errors: list[str], prefix: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{prefix}'{name}' must be a list of tier ids, got {type(value).__name__}.")
        return []
    out = []
    for item in value:
        if item not in TIER_ORDER:
            errors.append(f"{prefix}'{name}' contains unknown tier '{item}'.")
        else:
            out.append(item)
    return out


def _check_body(body: object, errors: list[str], prefix: str, *, top: bool,
                budget: float, running: float) -> tuple[int, float]:
    """Validate a pipeline body. Returns (max gate severity, total estimated cost)."""
    if not isinstance(body, dict):
        errors.append(f"{prefix}pipeline must be a JSON object.")
        return 0, 0.0

    required = REQUIRED_TOP if top else ("tiers",)
    for field in required:
        if field not in body:
            errors.append(f"{prefix}Missing required field: '{field}'")
    tiers = body.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        errors.append(f"{prefix}'tiers' must be a non-empty list of tier objects.")
        return 0, 0.0

    entry = body.get("entry_tier", "L1" if not top else None)
    if entry is not None and entry not in TIER_ORDER:
        errors.append(f"{prefix}Invalid entry_tier '{entry}'. Must be one of {list(TIER_ORDER)}")
        entry = None
    satisfied = _tier_list(body.get("satisfied_tiers"), "satisfied_tiers", errors, prefix)
    if len(set(satisfied)) != len(satisfied):
        errors.append(f"{prefix}'satisfied_tiers' contains duplicates.")
    if entry:
        for s in satisfied:
            if TIER_ORDER[s] >= TIER_ORDER[entry]:
                errors.append(f"{prefix}Satisfied tier {s} must come before entry_tier {entry}.")

    planned_ids = [t.get("tier_id") for t in tiers if isinstance(t, dict)]
    planned = {t for t in planned_ids if t in TIER_ORDER}
    available = planned | set(satisfied)

    node_id_to_tier: dict[str, str] = {}
    for idx, t in enumerate(tiers):
        if isinstance(t, dict) and "node_id" in t:
            nid = t.get("node_id")
            if not isinstance(nid, str) or not nid.strip():
                errors.append(f"{prefix}Tier index {idx} 'node_id' must be a non-empty string.")
            elif nid in node_id_to_tier:
                errors.append(f"{prefix}Duplicate node_id '{nid}'.")
            else:
                node_id_to_tier[nid] = t.get("tier_id")

    seen: set[str] = set()
    last_order = 0
    max_sev = 0
    total_cost = 0.0
    spent = running

    for idx, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            errors.append(f"{prefix}Tier index {idx} must be an object, got {type(tier).__name__}.")
            continue
        tid = tier.get("tier_id")
        if tid not in TIER_ORDER:
            errors.append(f"{prefix}Tier index {idx} has invalid tier_id '{tid}'.")
            continue
        label = f"{prefix}Tier {tid}"

        if tid in seen:
            errors.append(f"{label} appears more than once; merge its tools into one entry.")
        seen.add(tid)
        if TIER_ORDER[tid] < last_order:
            errors.append(f"{label} is listed out of order; list tiers in L1..L5 order.")
        last_order = max(last_order, TIER_ORDER[tid])
        if tid in satisfied:
            errors.append(f"{label} is both satisfied by the user and planned.")
        if entry and TIER_ORDER[tid] < TIER_ORDER[entry] and tid not in satisfied:
            errors.append(f"{label} precedes entry_tier {entry}; add it to satisfied_tiers or move entry_tier.")

        for field in REQUIRED_TIER:
            if field not in tier:
                errors.append(f"{label} is missing required field '{field}'.")
        for field in ("role", "expected_output"):
            if field in tier and (not isinstance(tier[field], str) or not tier[field].strip()):
                errors.append(f"{label} '{field}' must be a non-empty string.")
        mode = tier.get("execution_mode")
        if "execution_mode" in tier and mode not in EXECUTION_MODES:
            errors.append(f"{label} has invalid execution_mode '{mode}'. Valid: {sorted(EXECUTION_MODES)}")

        gate = tier.get("gate")
        if "gate" in tier and gate not in GATE_SEVERITY:
            errors.append(f"{label} has invalid gate '{gate}'. Valid: {sorted(GATE_SEVERITY)}")
            gate = None

        tools = tier.get("tools")
        tool_names: list[str] = []
        if "tools" in tier:
            if not isinstance(tools, list) or not tools:
                errors.append(f"{label} must list at least one Skill or MCP in 'tools'.")
            elif not all(isinstance(t, str) and t.strip() for t in tools):
                errors.append(f"{label} 'tools' must contain non-empty strings only.")
            else:
                tool_names = list(tools)

        if "node_id" in tier:
            nid = tier.get("node_id")
            if not isinstance(nid, str) or not nid.strip():
                errors.append(f"{label} 'node_id' must be a non-empty string.")

        if "fallback" in tier:
            fb = tier.get("fallback")
            if not isinstance(fb, list):
                errors.append(f"{label} 'fallback' must be a list of tool names.")
            elif not all(isinstance(f, str) and f.strip() for f in fb):
                errors.append(f"{label} 'fallback' must contain non-empty strings only.")

        deps = tier.get("depends_on", [])
        if not isinstance(deps, list):
            errors.append(f"{label} 'depends_on' must be a list of tier ids, got {type(deps).__name__}.")
            deps = []
        for dep in deps:
            if dep in TIER_ORDER:
                if TIER_ORDER[dep] >= TIER_ORDER[tid]:
                    errors.append(f"Invalid dependency: {tid} cannot depend on same or downstream tier '{dep}'.")
                elif dep not in available:
                    errors.append(f"{label} depends on {dep}, which is neither in this plan nor in satisfied_tiers.")
            elif dep in node_id_to_tier:
                dep_tier = node_id_to_tier[dep]
                if dep_tier and dep_tier in TIER_ORDER and TIER_ORDER[dep_tier] >= TIER_ORDER[tid]:
                    errors.append(f"Invalid dependency: {tid} cannot depend on same or downstream tier '{dep}'.")
            else:
                errors.append(f"{label} depends on unknown tier '{dep}'.")

        rb = tier.get("rollback_target")
        if rb is not None:
            if rb not in TIER_ORDER:
                errors.append(f"{label} has unknown rollback_target '{rb}'.")
            elif TIER_ORDER[rb] >= TIER_ORDER[tid]:
                errors.append(f"{label} rollback_target '{rb}' must be an upstream tier.")
            elif rb not in available:
                errors.append(f"{label} rollback_target '{rb}' is not in this plan or satisfied_tiers.")

        # ---- cost bookkeeping -------------------------------------------------
        raw_cost = tier.get("estimated_cost_usd")
        cost: float | None = None
        if "estimated_cost_usd" in tier:
            if _is_number(raw_cost):
                cost = float(raw_cost)
            else:
                errors.append(f"{label} 'estimated_cost_usd' must be a non-negative number.")

        sub_sev, sub_total = 0, 0.0
        if "sub_pipeline" in tier:
            sub_sev, sub_total = _check_body(tier["sub_pipeline"], errors, f"{label} > sub-pipeline: ",
                                             top=False, budget=budget, running=spent)
            if cost is not None and sub_total > cost + EPS:
                errors.append(f"{label} estimated_cost_usd ({cost:g}) is below the sum of its "
                              f"sub-pipeline estimates ({sub_total:g}); costs must bubble up.")
            if gate in GATE_SEVERITY and sub_sev > GATE_SEVERITY[gate]:
                errors.append(f"{label} gate '{gate}' is weaker than a gate inside its sub-pipeline; "
                              "sub-pipeline gates must bubble up to the parent tier.")

        effective = max(cost or 0.0, sub_total)
        spent += effective
        total_cost += effective

        # ---- gate rules -------------------------------------------------------
        if tid == "L5" and gate in GATE_SEVERITY and gate != "high_risk_confirm":
            errors.append(f"Safety violation: Tier L5 (External Delivery/Mutation) must use "
                          f"'high_risk_confirm', not '{gate}'.")

        risky = {n: classify_tool_name(n) for n in tool_names}
        for name, sig in risky.items():
            if sig["risk"] and gate in GATE_SEVERITY and gate != "high_risk_confirm":
                errors.append(f"{label} tool '{name}' signals an external or destructive action "
                              f"({', '.join(sig['risk'])}) but gate is '{gate}'; use 'high_risk_confirm'.")

        paid = [n for n, sig in risky.items() if sig["cost"]]
        if gate == "auto":
            if paid and cost is None and sub_total == 0:
                errors.append(f"{label} runs paid tool(s) {paid} under gate 'auto' without "
                              "'estimated_cost_usd'; add an estimate and approved_budget_usd, or use 'cost_confirm'.")
            elif effective > 0 and spent > budget + EPS:
                errors.append(f"{label} gate is 'auto' but the cumulative estimate ${spent:g} exceeds "
                              f"approved_budget_usd ${budget:g}; use 'cost_confirm' or raise the approved budget.")
        elif gate == "cost_confirm" and cost is None and sub_total == 0:
            errors.append(f"{label} uses 'cost_confirm' but has no 'estimated_cost_usd' to show the user.")

        if gate in GATE_SEVERITY:
            max_sev = max(max_sev, GATE_SEVERITY[gate])
        max_sev = max(max_sev, sub_sev)

    if "L2b" in planned and ("L2a" in planned or "L2a" in satisfied):
        l2b = next((t for t in tiers if isinstance(t, dict) and t.get("tier_id") == "L2b"), {})
        l2b_deps = l2b.get("depends_on", [])
        if not isinstance(l2b_deps, list) or "L2a" not in l2b_deps:
            errors.append(f"{prefix}L2 Race Condition: both L2a and L2b exist, but L2b does not list "
                          "'L2a' in 'depends_on'.")
    return max_sev, total_cost


def validate_pipeline(data: object) -> list[str]:
    errors: list[str] = []
    budget = 0.0
    if isinstance(data, dict):
        if "approved_budget_usd" in data:
            if _is_number(data["approved_budget_usd"]):
                budget = float(data["approved_budget_usd"])
            else:
                errors.append("'approved_budget_usd' must be a non-negative number.")
        if "max_retries" in data:
            mr = data["max_retries"]
            if isinstance(mr, bool) or not isinstance(mr, int) or not 0 <= mr <= 5:
                errors.append("'max_retries' must be an integer between 0 and 5.")
        if "macro_mode" in data and not isinstance(data["macro_mode"], bool):
            errors.append("'macro_mode' must be true or false.")
        if "ambient_modifiers" in data and not (
            isinstance(data["ambient_modifiers"], list)
            and all(isinstance(m, str) for m in data["ambient_modifiers"])
        ):
            errors.append("'ambient_modifiers' must be a list of strings.")
    _check_body(data, errors, "", top=True, budget=budget, running=0.0)
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print("Usage: python3 scripts/validate_pipeline.py <pipeline.json>\n"
              "Exit codes: 0 valid, 1 plan has issues, 2 usage/file/JSON error.")
        return 0
    if len(argv) != 1:
        print("Usage: python3 scripts/validate_pipeline.py <pipeline.json>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Error parsing JSON: {exc}", file=sys.stderr)
        return 2

    issues = validate_pipeline(payload)
    if issues:
        print("[L0-Validator] FAILED with issues:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    scope = payload.get("scope") if isinstance(payload, dict) else ""
    print(f"[L0-Validator] PASSED: '{scope}' (Entry: {payload.get('entry_tier')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
