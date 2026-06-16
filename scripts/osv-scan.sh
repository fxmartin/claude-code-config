#!/usr/bin/env bash
#
# ABOUTME: osv-scanner dependency gate (Story 9.1-002) — scan a tree and classify
# ABOUTME: the JSON report into a CLEAN | WARN | BLOCK verdict via `sdlc depscan`.
#
# Runs an osv-scanner scan over a target path (auto-detecting lockfiles) and
# classifies the JSON report into a CLEAN | WARN | BLOCK verdict via the
# controller's `sdlc depscan` command. The coverage gate calls this after
# coverage is measured; the orchestrator treats BLOCK (exit 1) as a build
# failure and routes to the bugfix loop.
#
# Severity mapping (OSV -> verdict):
#   HIGH / CRITICAL    -> BLOCK  (gate fails, exit 1)
#   LOW / MODERATE     -> WARN   (advisory, exit 0)
#   none / unknown     -> WARN   (a known finding never silently passes)
#
# Per-repo suppressions live in .dep-scan-suppressions.yaml at REPO_ROOT
# (suppress findings by OSV ID with a mandatory reason and expiry; an expired
# suppression fails the gate with exit 2).
#
# Modes:
#   osv-scan.sh [TARGET]            scan TARGET (default ".") and classify
#   osv-scan.sh --report FILE       classify an existing osv-scanner report (no scan)
#
# Output:
#   DEP_SCAN_STATUS: CLEAN | WARN | BLOCK   (plus one line per gating finding)
#
# Exit status:
#   0  CLEAN or WARN
#   1  BLOCK
#   2  usage / environment error (osv-scanner missing, bad report, expired suppression)
#
# Environment:
#   OSV_REPORT_PATH    where osv-scanner writes its JSON report
#                      (default: a mktemp file, cleaned up on exit)
#   SDLC_BIN           how to invoke the controller (default: "uv run sdlc",
#                      falling back to "sdlc" on PATH)

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

suppressions_file="${repo_root}/.dep-scan-suppressions.yaml"

# Resolve how we call the controller. Prefer an explicit override, then the
# packaged `sdlc` on PATH, then `uv run sdlc` from the controller project.
classify_report() {
  local report_file="$1"
  local config_args=()
  if [ -f "${suppressions_file}" ]; then
    config_args=(--suppressions "${suppressions_file}")
  fi

  if [ -n "${SDLC_BIN:-}" ]; then
    # shellcheck disable=SC2086
    ${SDLC_BIN} depscan "${config_args[@]}" "${report_file}"
  elif command -v sdlc >/dev/null 2>&1; then
    sdlc depscan "${config_args[@]}" "${report_file}"
  else
    (cd "${repo_root}/controller" && uv run sdlc depscan "${config_args[@]}" "${report_file}")
  fi
}

# --- Mode: classify an existing report (used by tests / re-runs) ------------
if [ "${1:-}" = "--report" ]; then
  report_file="${2:-}"
  if [ -z "${report_file}" ] || [ ! -f "${report_file}" ]; then
    echo "error: --report requires an existing report file" >&2
    exit 2
  fi
  classify_report "${report_file}"
  exit $?
fi

# --- Mode: run osv-scanner then classify ------------------------------------
target="${1:-.}"

if ! command -v osv-scanner >/dev/null 2>&1; then
  echo "error: osv-scanner not found on PATH; install it or run with --report" >&2
  exit 2
fi

cleanup_report=0
report_path="${OSV_REPORT_PATH:-}"
if [ -z "${report_path}" ]; then
  report_path="$(mktemp -t osv-report.XXXXXX.json)"
  cleanup_report=1
fi
trap '[ "${cleanup_report}" -eq 1 ] && rm -f "${report_path}"' EXIT

# osv-scanner returns non-zero when it finds vulnerabilities; that is expected,
# not an error, so guard the call. A genuine osv-scanner failure (bad lockfile,
# crash) leaves no readable report, which the classifier rejects with exit 2.
osv-scanner \
  --lockfile=auto \
  --format=json \
  --output="${report_path}" \
  "${target}" >/dev/null 2>&1 || true

classify_report "${report_path}"
