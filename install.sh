#!/usr/bin/env bash
# Installs eddyy-skill-orchestrator into the skill directories of the agents found on this machine.
# Usage: bash install.sh [--target DIR]... [--no-scan]
#   --target DIR   install into DIR/eddyy-skill-orchestrator (repeatable; replaces auto-detection)
#   --no-scan      skip the initial inventory scan
# Env: L0_SKILL_DIRS="dirA;dirB" (":" on Unix) works like repeated --target.
#      L0_SKILL_ROOTS="dirA;dirB" (scanner) tells scan_inventory.py where host skills live.
#
# Upgrades never delete: the new version is staged, the previous install is
# renamed to <dest>.old-<timestamp> and kept, and the staged copy is moved into
# place. Remove the kept directory yourself when you no longer need it.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="eddyy-skill-orchestrator"
TARGETS=()
SCAN=1

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      [ $# -ge 2 ] || { echo "--target needs a directory" >&2; exit 2; }
      TARGETS+=("$2"); shift 2 ;;
    --no-scan) SCAN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Path separator: ":" on Unix, ";" on MSYS/Git-for-Windows so drive letters survive.
case "$(uname -s 2>/dev/null || echo '')" in
  MINGW*|MSYS*|CYGWIN*) SEP=';' ;;
  *) SEP=':' ;;
esac

if [ ${#TARGETS[@]} -eq 0 ] && [ -n "${L0_SKILL_DIRS:-}" ]; then
  IFS="$SEP" read -r -a TARGETS <<< "${L0_SKILL_DIRS}"
fi
if [ ${#TARGETS[@]} -eq 0 ]; then
  # Auto-detect: only directories that already exist are added, so the installer
  # never creates empty agent dirs on machines that do not use them.
  for base in "${HOME}/.claude/skills" "${HOME}/.agents/skills" "${HOME}/.cursor/skills" "${HOME}/.codex/skills" "${HOME}/.workbuddy/skills"; do
    [ -d "${base}" ] && TARGETS+=("${base}")
  done
  if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "No skill directory found (looked in ~/.claude/skills, ~/.agents/skills, ~/.cursor/skills," >&2
    echo "~/.codex/skills, ~/.workbuddy/skills). Pass --target DIR, or set L0_SKILL_DIRS." >&2
    exit 3
  fi
fi

# Validate every target: no empties, no filesystem/drive roots.
for base in "${TARGETS[@]}"; do
  case "${base}" in
    ""|"/"|"/"[[:alpha:]]) echo "Refusing unsafe target: '${base}'" >&2; exit 2 ;;
    ?:) echo "Refusing unsafe target: '${base}'" >&2; exit 2 ;;
  esac
done

echo "Installing ${SKILL_NAME} ..."
FIRST_DEST=""
TS="$(date +%Y%m%d%H%M%S)"
for base in "${TARGETS[@]}"; do
  dest="${base}/${SKILL_NAME}"
  [ "$(basename "${dest}")" = "${SKILL_NAME}" ] || { echo "refusing unexpected path: ${dest}" >&2; exit 1; }
  # Stage into a fresh directory, then swap. Nothing is deleted during the
  # upgrade: agent hosts often intercept `rm` behind an approval prompt, and a
  # blocked install is worse than a leftover directory.
  stage="${dest}.new-${TS}"
  if [ -e "${stage}" ]; then
    echo "  staging path exists, using a unique name" >&2
    stage="${dest}.new-${TS}-$$"
  fi
  mkdir -p "${stage}"
  cp "${REPO_DIR}/SKILL.md" "${stage}/SKILL.md"
  for sub in references assets; do
    [ -d "${REPO_DIR}/${sub}" ] && cp -R "${REPO_DIR}/${sub}" "${stage}/"
  done
  # Copy scripts file by file so no __pycache__ ever lands in the install.
  if [ -d "${REPO_DIR}/scripts" ]; then
    mkdir -p "${stage}/scripts"
    for f in "${REPO_DIR}"/scripts/*.py; do
      [ -e "$f" ] && cp "$f" "${stage}/scripts/"
    done
  fi
  if [ -e "${dest}/SKILL.md" ]; then
    old="${dest}.old-${TS}"
    if [ -e "${old}" ]; then old="${dest}.old-${TS}-$$"; fi
    mv "${dest}" "${old}"
    echo "  [kept old] ${old}"
  fi
  mv "${stage}" "${dest}"
  echo "  [ok] ${dest}"
  [ -n "${FIRST_DEST}" ] || FIRST_DEST="${dest}"
done

if [ "${SCAN}" -eq 1 ]; then
  PY=""
  for cand in python3 python; do
    if command -v "${cand}" >/dev/null 2>&1 \
       && "${cand}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="${cand}"; break
    fi
  done
  if [ -n "${PY}" ]; then
    echo "Scanning local skills and MCP configs ..."
    "${PY}" "${FIRST_DEST}/scripts/scan_inventory.py" "${FIRST_DEST}/.l0-inventory.json" \
      || echo "WARNING: inventory scan failed; the skill still works without it." >&2
    echo "Tip: if host skills were missed, re-run the scanner with L0_SKILL_ROOTS set:" >&2
    echo "  L0_SKILL_ROOTS=\"/path/to/skills\" ${PY} ${FIRST_DEST}/scripts/scan_inventory.py" >&2
  else
    echo "WARNING: python >= 3.9 not found; scripts/ need it. SKILL.md works without them." >&2
  fi
fi

echo "Done. Installed into ${#TARGETS[@]} location(s)."
