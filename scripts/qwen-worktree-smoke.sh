#!/usr/bin/env bash
#
# ABOUTME: Story 29.1-001 evidence script — proves (or disproves) that `qwen -p`
# ABOUTME: completes a small edit task unattended inside a fresh git worktree.
#
# Cuts N worktrees off a throwaway scratch repo (never the real repo this
# script lives in — mirrors the hermeticity guards in
# controller/tests/conftest.py), runs scripts/qwen-build-adapter.sh in each
# worktree concurrently with cwd set to that worktree (matching how the
# controller's dispatch layer invokes a harness), and checks:
#   - each worktree's edit lands only in that worktree
#   - the scratch repo root stays untouched
#   - one worktree's failure never leaks into another
#
# A structured evidence record (qwen version, flags, per-worktree result,
# isolation check, overall PASS/FAIL) is written to --evidence-out so a run is
# reproducible and citable from harnesses.yaml.
#
# Usage:
#   qwen-worktree-smoke.sh [--worktrees N] [--sandbox DIR] [--evidence-out FILE]
#
# Environment:
#   QWEN_FLAGS  Forwarded to scripts/qwen-build-adapter.sh (e.g. '--yolo' for
#               unattended approval — see that script's own docs).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="${SCRIPT_DIR}/qwen-build-adapter.sh"

num_worktrees=2
sandbox=""
evidence_out=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worktrees) num_worktrees="$2"; shift 2 ;;
    --sandbox) sandbox="$2"; shift 2 ;;
    --evidence-out) evidence_out="$2"; shift 2 ;;
    *) echo "qwen-worktree-smoke: unknown argument: $1" >&2; exit 2 ;;
  esac
done

sandbox="${sandbox:-$(mktemp -d)}"
evidence_out="${evidence_out:-${sandbox}/evidence.json}"
mkdir -p "$sandbox"

repo="${sandbox}/scratch-repo"
mkdir -p "$repo"
git -C "$repo" init -q -b main
git -C "$repo" config user.email smoke@example.test
git -C "$repo" config user.name smoke
echo seed > "${repo}/seed.txt"
git -C "$repo" add -A
git -C "$repo" commit -q -m seed

# Run the version probe from the sandbox, never this script's caller's cwd —
# defense in depth against a misbehaving `qwen --version` writing anywhere
# (see Story 29.1-001: an earlier version of this script's own test double hit
# exactly this by writing unconditionally, leaking a file into the real repo).
qwen_version="$(cd "$sandbox" && "${QWEN_BIN:-qwen}" --version 2>/dev/null || echo unknown)"
qwen_flags="${QWEN_FLAGS:-}"

declare -a worktree_dirs=()
declare -a exit_codes=()
declare -a stderr_files=()

prompt="Create a file named edited-by-qwen.txt containing the text OK and nothing else."

for ((i = 1; i <= num_worktrees; i++)); do
  wt="${sandbox}/wt-${i}"
  git -C "$repo" worktree add -q -b "smoke/wt-${i}" "$wt" >/dev/null
  worktree_dirs+=("$wt")
  stderr_files+=("${sandbox}/wt-${i}.stderr")
done

pids=()
for ((i = 0; i < num_worktrees; i++)); do
  (
    cd "${worktree_dirs[$i]}"
    printf '%s' "$prompt" | QWEN_FLAGS="$qwen_flags" bash "$ADAPTER" \
      >"${sandbox}/wt-$((i + 1)).stdout" 2>"${stderr_files[$i]}"
  ) &
  pids+=("$!")
done

overall_status=0
for ((i = 0; i < num_worktrees; i++)); do
  if wait "${pids[$i]}"; then
    exit_codes+=(0)
  else
    exit_codes+=("$?")
    overall_status=1
  fi
done

isolation_ok=true
for ((i = 0; i < num_worktrees; i++)); do
  wt="${worktree_dirs[$i]}"
  if [[ "${exit_codes[$i]}" -eq 0 && ! -f "${wt}/edited-by-qwen.txt" ]]; then
    isolation_ok=false
  fi
  for ((j = 0; j < num_worktrees; j++)); do
    [[ "$i" -eq "$j" ]] && continue
    other="${worktree_dirs[$j]}"
    if [[ -f "${wt}/edited-by-qwen.txt" && "${exit_codes[$j]}" -ne 0 ]]; then
      # A successful worktree's file must never appear in a failed sibling.
      [[ -f "${other}/edited-by-qwen.txt" ]] && isolation_ok=false
    fi
  done
done
if [[ -n "$(git -C "$repo" status --porcelain)" ]]; then
  isolation_ok=false
fi

overall="PASS"
[[ "$overall_status" -ne 0 || "$isolation_ok" != true ]] && overall="FAIL"

{
  echo "{"
  echo "  \"qwen_version\": \"${qwen_version}\","
  echo "  \"flags\": \"${qwen_flags}\","
  echo "  \"worktrees\": ["
  for ((i = 0; i < num_worktrees; i++)); do
    stderr_snippet="$(tr '\n' ' ' < "${stderr_files[$i]}" 2>/dev/null | head -c 500)"
    comma=","
    [[ $((i + 1)) -eq "$num_worktrees" ]] && comma=""
    echo "    {\"worktree\": \"wt-$((i + 1))\", \"exit_code\": ${exit_codes[$i]}, \"stderr\": \"${stderr_snippet//\"/\\\"}\"}${comma}"
  done
  echo "  ],"
  echo "  \"isolation_ok\": ${isolation_ok},"
  echo "  \"overall\": \"${overall}\""
  echo "}"
} > "$evidence_out"

[[ "$overall" == "PASS" ]] && exit 0
exit 1
