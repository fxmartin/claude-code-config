#!/usr/bin/env bash
#
# run-type-check.sh — static type-check gate for the controller (Issue #586).
#
# Runs mypy over controller/src using the config in controller/pyproject.toml,
# then ratchets the result against controller/.mypy-baseline.json via the
# controller's `sdlc typecheck` command. New violations fail the build; the
# known backlog does not.
#
# The strictness ladder lives in docs/python-best-practices.md:
#   rung 0  every module — real-defect checks, annotation coverage optional
#   rung 1  [[tool.mypy.overrides]] allowlist — full strict
#
# Modes:
#   run-type-check.sh                run mypy and gate against the baseline
#   run-type-check.sh --update       run mypy and rewrite the baseline
#   run-type-check.sh --report FILE  gate an existing mypy report (no run)
#
# Output:
#   TYPE_CHECK_STATUS: CLEAN | WARN | BLOCK   (plus one line per new violation)
#
# Exit status:
#   0  CLEAN or WARN
#   1  BLOCK (a violation beyond the baseline)
#   2  usage / environment error (mypy missing or crashed, bad baseline)
#
# Environment:
#   SDLC_BIN   how to invoke the controller (default: the `sdlc` on PATH,
#              falling back to `uv run --project controller sdlc`)

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

controller_dir="${repo_root}/controller"
baseline_file="${controller_dir}/.mypy-baseline.json"

# Resolve how we call the controller. Unlike the other gates, this one checks
# the controller's *own* source tree, so the working copy wins over any `sdlc`
# already installed on PATH — a stale global install would otherwise silently
# gate the branch with last release's ratchet.
ratchet() {
  local report_file="$1"
  shift

  if [ -n "${SDLC_BIN:-}" ]; then
    # shellcheck disable=SC2086
    ${SDLC_BIN} typecheck --baseline "${baseline_file}" "$@" "${report_file}"
  elif [ -f "${controller_dir}/pyproject.toml" ] && command -v uv > /dev/null 2>&1; then
    uv run --project "${controller_dir}" sdlc typecheck \
      --baseline "${baseline_file}" "$@" "${report_file}"
  elif command -v sdlc > /dev/null 2>&1; then
    sdlc typecheck --baseline "${baseline_file}" "$@" "${report_file}"
  else
    echo "error: no way to invoke the controller (need uv or sdlc on PATH)" >&2
    exit 2
  fi
}

# --- Mode: gate an existing report (used by tests / re-runs) ----------------
if [ "${1:-}" = "--report" ]; then
  report_path="${2:-}"
  if [ -z "${report_path}" ] || [ ! -f "${report_path}" ]; then
    echo "error: --report requires an existing report file" >&2
    exit 2
  fi
  ratchet "${report_path}"
  exit $?
fi

update_args=()
if [ "${1:-}" = "--update" ]; then
  update_args=(--update)
elif [ "$#" -gt 0 ]; then
  echo "error: unknown argument '${1}' (expected --update or --report FILE)" >&2
  exit 2
fi

# --- Mode: run mypy then gate ----------------------------------------------
report_path="$(mktemp -t mypy-report.XXXXXX.txt)"
trap 'rm -f "${report_path}"' EXIT

# mypy exits 1 when it finds errors, which is the expected normal path here, so
# the call must not trip `set -e`. Exit 2 is a genuine failure (bad config,
# missing files, crash) and there is no trustworthy report to ratchet against.
mypy_status=0
(cd "${controller_dir}" && uv run mypy --no-color-output --no-error-summary) \
  > "${report_path}" 2>&1 || mypy_status=$?

if [ "${mypy_status}" -gt 1 ]; then
  echo "error: mypy failed to run (exit ${mypy_status})" >&2
  cat "${report_path}" >&2
  exit 2
fi

# Paths in the report are relative to controller/, which is where the baseline
# signatures were captured — gate from there so the two always agree.
ratchet "${report_path}" "${update_args[@]}"
