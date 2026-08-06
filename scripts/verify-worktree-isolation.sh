#!/usr/bin/env bash
#
# ABOUTME: Reproducible evidence generator for Epic-29 (harness worktree isolation).
# ABOUTME: Proves an adapter writes unattended inside a git worktree without cross-contamination.
#
# Story 29.1-001 (qwen) and 29.2-002 (opencode) both require *recorded evidence*
# that a harness adapter completes an edit task unattended inside a freshly cut
# git worktree, with two concurrent worktrees not leaking into each other or the
# shared repo root, BEFORE the `worktree_isolation` / `parallel` capability flags
# in `controller/src/sdlc/config/harnesses.yaml` may be flipped to true
# (conservative-by-default rule, capability.py). This script produces that
# evidence and is the shared verification surface for every candidate harness —
# parameterize it by `--adapter`, never duplicate it per harness.
#
# HERMETICITY: this script NEVER touches the current repository. It creates its
# own throwaway git repo under a mktemp dir, cuts worktrees there, and removes
# everything on exit. It is a manual evidence tool — it must never run in CI
# against the real tree (the real adapters call live model endpoints).
#
# Usage:
#   scripts/verify-worktree-isolation.sh                     # default: qwen adapter
#   scripts/verify-worktree-isolation.sh --adapter scripts/opencode-build-adapter.sh
#   scripts/verify-worktree-isolation.sh --adapter <path> --label opencode
#   scripts/verify-worktree-isolation.sh --keep              # keep scratch dir for inspection
#
# Exit status:
#   0  isolation verified (single + concurrent) — paste the EVIDENCE block into
#      the story note, then the flags may be flipped.
#   1  isolation FAILED / verified-negative — flags must stay false; the report
#      says which assertion failed.
#   2  usage / precondition error (bad args, missing adapter, git unavailable).
#
# The adapter contract this harness expects (same for every harness):
#   * The role prompt is delivered on the adapter's stdin.
#   * The adapter runs with cwd = the worktree it must edit.
#   * The task is to CREATE the marker file named in the prompt containing the
#     unique token in the prompt. The marker path + token are ALSO exported as
#     VERIFY_MARKER_FILE / VERIFY_MARKER_TOKEN so a deterministic fake adapter
#     can satisfy the contract without a model (used by the hermetic unit test);
#     real model adapters ignore the env and act on the prompt text.
#   * A zero exit means the adapter believes it finished. This script does not
#     trust that — it verifies the filesystem effect independently.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ADAPTER="${SCRIPT_DIR}/qwen-build-adapter.sh"
LABEL=""
KEEP=0

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --adapter) ADAPTER="${2:?--adapter needs a path}"; shift 2 ;;
    --label)   LABEL="${2:?--label needs a value}"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "verify-worktree-isolation: unexpected argument: $1" >&2; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo "verify-worktree-isolation: git not found" >&2; exit 2; }
[[ -x "$ADAPTER" ]] || { echo "verify-worktree-isolation: adapter not executable: $ADAPTER" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="$(basename "$ADAPTER" | sed 's/-build-adapter\.sh$//; s/\.sh$//')"

# --- throwaway scratch repo (never the caller's tree) ------------------------
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/verify-worktree.XXXXXX")"
cleanup() { [[ "$KEEP" -eq 1 ]] || rm -rf "$SCRATCH"; }
trap cleanup EXIT

ROOT="${SCRATCH}/repo"
git init -q "$ROOT"
git -C "$ROOT" -c user.email=verify@sdlc.local -c user.name=verify commit -q \
  --allow-empty -m "seed" 2>/dev/null || true
echo "seed" > "${ROOT}/seed.txt"
git -C "$ROOT" add seed.txt
git -C "$ROOT" -c user.email=verify@sdlc.local -c user.name=verify commit -q -m "seed"

BASE_REF="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"

# Per-slot state, keyed by slot name — avoids indirect/eval variable access.
declare -A WT MARKER PID

# --- run one adapter task inside its own worktree ----------------------------
# Args: <slot-name>  -> creates worktree, drives the adapter, records exit code.
run_slot() {
  local slot="$1"
  local wt="${SCRATCH}/wt-${slot}"
  local branch="verify/${slot}"
  local marker="MARKER_${slot}.txt"
  local token="tok-${slot}-$$"

  git -C "$ROOT" worktree add -q -b "$branch" "$wt" "$BASE_REF"

  local prompt
  prompt="You are running inside an isolated git worktree. Create a new file
named exactly '${marker}' in the current directory whose only contents are the
line '${token}'. Do not modify any other file. Then emit the RESULT_JSON block."

  # Env mirror of the task so a deterministic fake adapter can satisfy the
  # contract without a model; real adapters act on the prompt text above.
  (
    cd "$wt"
    # export (not a command prefix) so the vars cross the pipe into the adapter
    export VERIFY_MARKER_FILE="$marker" VERIFY_MARKER_TOKEN="$token"
    printf '%s' "$prompt" | "$ADAPTER" >"${SCRATCH}/${slot}.out" 2>&1
  ) &
  PID[$slot]=$!
  WT[$slot]="$wt"
  MARKER[$slot]="$marker"
}

# --- assert isolation for a completed slot -----------------------------------
FAILURES=0
note() { printf '  %-5s %s\n' "$1" "$2"; }

assert_slot() {
  local slot="$1" other="$2"
  local wt="${WT[$slot]}" marker="${MARKER[$slot]}" other_marker="${MARKER[$other]}"

  if [[ -f "${wt}/${marker}" ]]; then
    note PASS "${slot}: own marker present in its worktree (${marker})"
  else
    note FAIL "${slot}: own marker MISSING — adapter did not write unattended"
    FAILURES=$((FAILURES + 1))
  fi

  if [[ -e "${wt}/${other_marker}" ]]; then
    note FAIL "${slot}: sibling marker ${other_marker} leaked into this worktree"
    FAILURES=$((FAILURES + 1))
  else
    note PASS "${slot}: sibling marker absent (no cross-contamination)"
  fi
}

# ---------------------------------------------------------------------------
echo "== worktree-isolation verification: harness='${LABEL}' adapter='${ADAPTER}'"

run_slot A
run_slot B
wait "${PID[A]}" && A_EXIT=0 || A_EXIT=$?
wait "${PID[B]}" && B_EXIT=0 || B_EXIT=$?

echo "-- adapter exit codes: A=${A_EXIT} B=${B_EXIT}"
[[ "$A_EXIT" -eq 0 ]] || { note FAIL "A: adapter exited ${A_EXIT}"; FAILURES=$((FAILURES + 1)); }
[[ "$B_EXIT" -eq 0 ]] || { note FAIL "B: adapter exited ${B_EXIT}"; FAILURES=$((FAILURES + 1)); }

echo "-- isolation assertions:"
assert_slot A B
assert_slot B A

# The shared repo ROOT working tree must be untouched by either agent.
if git -C "$ROOT" status --porcelain | grep -q .; then
  note FAIL "shared repo root working tree was modified"
  FAILURES=$((FAILURES + 1))
else
  note PASS "shared repo root untouched"
fi

# --- evidence block (paste into the story note on success) -------------------
TOOL_VERSION="$("${ADAPTER}" --version 2>/dev/null | head -1 || true)"
echo
echo "-- EVIDENCE (record in the story note before flipping flags):"
echo "   harness:        ${LABEL}"
echo "   adapter:        ${ADAPTER}"
echo "   adapter flags:  QWEN_FLAGS='${QWEN_FLAGS:-}' OPENCODE_FLAGS='${OPENCODE_FLAGS:-}'"
echo "   tool --version: ${TOOL_VERSION:-<adapter has no --version passthrough>}"
echo "   concurrency:    2 worktrees, cut from '${BASE_REF}', run simultaneously"

if [[ "$FAILURES" -eq 0 ]]; then
  echo
  echo "RESULT: PASS — worktree isolation verified. The '${LABEL}' entry in"
  echo "harnesses.yaml may now set worktree_isolation:true + parallel:true,"
  echo "with a comment citing this evidence (date + tool version)."
  exit 0
fi

echo
echo "RESULT: FAIL (${FAILURES} assertion(s)) — verified-negative. Keep the"
echo "'${LABEL}' flags false and document the failure in the harness entry."
echo "(Adapter output: ${SCRATCH}/A.out, ${SCRATCH}/B.out$([[ "$KEEP" -eq 1 ]] && echo ' — kept')"
exit 1
