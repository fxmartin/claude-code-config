#!/usr/bin/env bash
#
# supply-chain-scan.sh — Supply-chain pattern gate (Story 13.2-001).
#
# Treats installed hooks, skills, MCP config, and settings as supply-chain
# artifacts and scans them for dangerous patterns via the controller's
# `sdlc supplychain` command. CI gates on the verdict: a BLOCK (exit 1) fails
# the PR so a poisoned config never merges unreviewed.
#
# Verdict mapping:
#   BLOCK  -> gate fails, exit 1  (pipe-to-shell, enableAllProjectMcpServers,
#                                  ANTHROPIC_BASE_URL, data:text/html, base64,,
#                                  zero-width/bidi Unicode)
#   WARN   -> advisory, exit 0    (plain curl/wget/nc/scp/ssh egress tools)
#   CLEAN  -> exit 0
#
# Per-finding overrides live in .supply-chain-allowlist.yaml at REPO_ROOT: each
# entry names a specific path + pattern id and a mandatory reason (no blanket
# disable).
#
# Usage:
#   supply-chain-scan.sh [ROOT]      scan ROOT (default: repo root) and classify
#
# Output:
#   SUPPLY_CHAIN_STATUS: CLEAN | WARN | BLOCK  (plus one line per gating finding)
#
# Exit status:
#   0  CLEAN or WARN
#   1  BLOCK
#   2  usage / environment error (malformed allowlist, etc.)
#
# Environment:
#   SDLC_BIN   how to invoke the controller (default: packaged `sdlc` on PATH,
#              falling back to `uv run sdlc` from the controller project)

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

target_root="${1:-${repo_root}}"
allowlist_file="${repo_root}/.supply-chain-allowlist.yaml"

allow_args=()
if [ -f "${allowlist_file}" ]; then
  allow_args=(--allowlist "${allowlist_file}")
fi

# Resolve how we call the controller. Prefer an explicit override, then the
# packaged `sdlc` on PATH, then `uv run sdlc` from the controller project.
if [ -n "${SDLC_BIN:-}" ]; then
  # shellcheck disable=SC2086
  ${SDLC_BIN} supplychain "${allow_args[@]}" "${target_root}"
elif command -v sdlc >/dev/null 2>&1; then
  sdlc supplychain "${allow_args[@]}" "${target_root}"
else
  (cd "${repo_root}/controller" && uv run sdlc supplychain "${allow_args[@]}" "${target_root}")
fi
