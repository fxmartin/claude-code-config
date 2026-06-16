#!/usr/bin/env bash
#
# sast-scan.sh — Semgrep SAST gate (Story 9.1-001).
#
# Runs a semgrep scan over a target path and classifies the JSON report into a
# CLEAN | WARN | BLOCK verdict via the controller's `sdlc sast` command. The
# coverage gate calls this after coverage is measured; the orchestrator treats
# BLOCK (exit 1) as a build failure and routes to the bugfix loop.
#
# Severity mapping (semgrep -> verdict):
#   ERROR    -> BLOCK  (gate fails, exit 1)
#   WARNING  -> WARN   (advisory, exit 0)
#   INFO     -> CLEAN  (ignored)
#
# Per-repo overrides live in .sast-config.yaml at REPO_ROOT (suppress findings
# by rule ID with a mandatory reason; append extra rulesets). Files in
# .semgrepignore are skipped by semgrep itself.
#
# Modes:
#   sast-scan.sh [TARGET]            scan TARGET (default ".") and classify
#   sast-scan.sh --report FILE       classify an existing semgrep report (no scan)
#
# Output:
#   SAST_STATUS: CLEAN | WARN | BLOCK   (plus one line per gating finding)
#
# Exit status:
#   0  CLEAN or WARN
#   1  BLOCK
#   2  usage / environment error (semgrep missing, bad report, etc.)
#
# Environment:
#   SAST_REPORT_PATH   where semgrep writes its JSON report
#                      (default: a mktemp file, cleaned up on exit)
#   SDLC_BIN           how to invoke the controller (default: "uv run sdlc",
#                      falling back to "sdlc" on PATH)

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

config_file="${repo_root}/.sast-config.yaml"

# Resolve how we call the controller. Prefer an explicit override, then the
# packaged `sdlc` on PATH, then `uv run sdlc` from the controller project.
classify_report() {
  local report_file="$1"
  local config_args=()
  if [ -f "${config_file}" ]; then
    config_args=(--config "${config_file}")
  fi

  if [ -n "${SDLC_BIN:-}" ]; then
    # shellcheck disable=SC2086
    ${SDLC_BIN} sast "${config_args[@]}" "${report_file}"
  elif command -v sdlc >/dev/null 2>&1; then
    sdlc sast "${config_args[@]}" "${report_file}"
  else
    (cd "${repo_root}/controller" && uv run sdlc sast "${config_args[@]}" "${report_file}")
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

# --- Mode: run semgrep then classify ----------------------------------------
target="${1:-.}"

if ! command -v semgrep >/dev/null 2>&1; then
  echo "error: semgrep not found on PATH; install it or run with --report" >&2
  exit 2
fi

cleanup_report=0
report_path="${SAST_REPORT_PATH:-}"
if [ -z "${report_path}" ]; then
  report_path="$(mktemp -t sast-report.XXXXXX.json)"
  cleanup_report=1
fi
trap '[ "${cleanup_report}" -eq 1 ] && rm -f "${report_path}"' EXIT

# semgrep returns non-zero when it finds matches; that is expected, not an
# error, so guard the call. A genuine semgrep failure (config error, crash)
# leaves no readable report, which the classifier rejects with exit 2.
semgrep \
  --config=p/default \
  --config=p/owasp-top-ten \
  --json \
  --output="${report_path}" \
  "${target}" >/dev/null 2>&1 || true

classify_report "${report_path}"
