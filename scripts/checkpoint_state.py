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
SCHEMA_VERSION = 2
TIER_RE = re.compile(r"^L(?:1|2a|2b|3|4|5)(?:\.\d+[ab]?)*$")  # nested ids like L3.2a allowed
BASE_ORDER = {"L1": 1, "L2a": 2, "L2b": 3, "L3": 4, "L4": 5, "L5": 6}
STATUSES = ("in_progress", "awaiting_confirmation", "completed", "rolled_back", "escalated")
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
    if state["status"] == "awaiting_confirmation":
        print(f"NEXT: ask the user for go/no-go before {state['current_tier']}, then run `confirm`.")
    elif state["status"] == "escalated":
        print("NEXT: retry limit hit. Report to the user; do not continue automatically.")
    elif state["status"] == "completed":
        print("NEXT: nothing, the pipeline is complete.")
    else:
        print(f"NEXT: continue at {state['current_tier']}.")


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
    return p


HANDLERS = {"init": cmd_init, "lock": cmd_lock, "complete-tier": cmd_complete,
            "confirm": cmd_confirm, "rollback": cmd_rollback, "show": cmd_show}


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
