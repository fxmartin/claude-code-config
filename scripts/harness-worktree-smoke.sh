#!/usr/bin/env bash
#
# ABOUTME: Shared worktree-isolation evidence script (Story 29.2-002), generalized
# ABOUTME: from the qwen-only 29.1-001 implementation by parameterizing on --harness.
#
# Proves (or disproves) that a harness's build adapter completes a small edit
# task unattended inside a fresh git worktree, and that two such worktrees
# running concurrently stay isolated from each other and from the scratch
# repo root. Both scripts/qwen-worktree-smoke.sh and
# scripts/opencode-worktree-smoke.sh are thin wrappers around this script —
# the scratch-repo / concurrent-worktree / isolation-check logic lives here
# exactly once so a third harness's evidence script is a wrapper, not a copy.
#
# Cuts N worktrees off a throwaway scratch repo (never the real repo this
# script lives in — mirrors the hermeticity guards in
# controller/tests/conftest.py), runs the harness's build adapter in each
# worktree concurrently with cwd set to that worktree (matching how the
# controller's dispatch layer invokes a harness), and checks:
#   - each worktree's edit lands only in that worktree
#   - the scratch repo root stays untouched
#   - one worktree's failure never leaks into another
#
# A structured evidence record (harness name + version, flags, per-worktree
# result, isolation check, overall PASS/FAIL) is written to --evidence-out so
# a run is reproducible and citable from harnesses.yaml.
#
# Usage:
#   harness-worktree-smoke.sh --harness NAME [--adapter PATH] [--bin NAME]
#     [--flags-env VARNAME] [--bin-env VARNAME] [--seed SRC:DEST]...
#     [--worktrees N] [--sandbox DIR] [--evidence-out FILE]
#
# Arguments:
#   --harness NAME     Required. Selects defaults below and names the edited
#                      probe file (edited-by-NAME.txt) and the evidence
#                      record's harness/version fields.
#   --adapter PATH     The build adapter to run in each worktree. Default:
#                      SCRIPT_DIR/NAME-build-adapter.sh.
#   --bin NAME         The CLI executable used for the `--version` evidence
#                      probe. Default: NAME.
#   --flags-env VAR    Env var forwarded to the adapter for CLI flags (read
#                      from this script's own environment). Default:
#                      upper(NAME)_FLAGS (e.g. QWEN_FLAGS, OPENCODE_FLAGS).
#   --bin-env VAR      Env var overriding the CLI executable, forwarded to
#                      both the version probe and the adapter. Default:
#                      upper(NAME)_BIN (e.g. QWEN_BIN, OPENCODE_BIN).
#   --seed SRC:DEST    Repeatable. Copies SRC into the scratch repo at DEST
#                      before the seed commit, so every worktree inherits it
#                      as a tracked file (e.g. an opencode.json permission
#                      config OpenCode requires for unattended writes).
#   --worktrees N      Number of concurrent worktrees to cut. Default: 2.
#   --sandbox DIR      Scratch sandbox root. Default: a fresh mktemp -d.
#   --evidence-out FILE  Where to write the evidence JSON. Default:
#                      SANDBOX/evidence.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

harness=""
adapter=""
bin=""
flags_env=""
bin_env=""
declare -a seed_specs=()
num_worktrees=2
sandbox=""
evidence_out=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness) harness="$2"; shift 2 ;;
    --adapter) adapter="$2"; shift 2 ;;
    --bin) bin="$2"; shift 2 ;;
    --flags-env) flags_env="$2"; shift 2 ;;
    --bin-env) bin_env="$2"; shift 2 ;;
    --seed) seed_specs+=("$2"); shift 2 ;;
    --worktrees) num_worktrees="$2"; shift 2 ;;
    --sandbox) sandbox="$2"; shift 2 ;;
    --evidence-out) evidence_out="$2"; shift 2 ;;
    *) echo "harness-worktree-smoke: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$harness" ]]; then
  echo "harness-worktree-smoke: --harness NAME is required" >&2
  exit 2
fi

harness_upper="$(printf '%s' "$harness" | tr '[:lower:]' '[:upper:]')"
adapter="${adapter:-${SCRIPT_DIR}/${harness}-build-adapter.sh}"
bin="${bin:-${harness}}"
flags_env="${flags_env:-${harness_upper}_FLAGS}"
bin_env="${bin_env:-${harness_upper}_BIN}"

sandbox="${sandbox:-$(mktemp -d)}"
evidence_out="${evidence_out:-${sandbox}/evidence.json}"
mkdir -p "$sandbox"

repo="${sandbox}/scratch-repo"
mkdir -p "$repo"
git -C "$repo" init -q -b main
git -C "$repo" config user.email smoke@example.test
git -C "$repo" config user.name smoke
echo seed > "${repo}/seed.txt"
for spec in ${seed_specs[@]+"${seed_specs[@]}"}; do
  src="${spec%%:*}"
  dest="${spec#*:}"
  mkdir -p "$(dirname "${repo}/${dest}")"
  cp "$src" "${repo}/${dest}"
done
git -C "$repo" add -A
git -C "$repo" commit -q -m seed

# Run the version probe from the sandbox, never this script's caller's cwd —
# defense in depth against a misbehaving `<bin> --version` writing anywhere
# (see Story 29.1-001: an earlier version of this script's own test double hit
# exactly this by writing unconditionally, leaking a file into the real repo).
resolved_bin="${!bin_env:-$bin}"
harness_version="$(cd "$sandbox" && "$resolved_bin" --version 2>/dev/null || echo unknown)"
harness_flags="${!flags_env:-}"

declare -a worktree_dirs=()
declare -a exit_codes=()
declare -a stderr_files=()

edited_file="edited-by-${harness}.txt"
prompt="Create a file named ${edited_file} containing the text OK and nothing else."

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
    printf '%s' "$prompt" | env "${flags_env}=${harness_flags}" "${bin_env}=${resolved_bin}" bash "$adapter" \
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
  if [[ "${exit_codes[$i]}" -eq 0 && ! -f "${wt}/${edited_file}" ]]; then
    isolation_ok=false
  fi
  for ((j = 0; j < num_worktrees; j++)); do
    [[ "$i" -eq "$j" ]] && continue
    other="${worktree_dirs[$j]}"
    if [[ -f "${wt}/${edited_file}" && "${exit_codes[$j]}" -ne 0 ]]; then
      # A successful worktree's file must never appear in a failed sibling.
      [[ -f "${other}/${edited_file}" ]] && isolation_ok=false
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
  echo "  \"harness\": \"${harness}\","
  echo "  \"${harness}_version\": \"${harness_version}\","
  echo "  \"flags\": \"${harness_flags}\","
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
