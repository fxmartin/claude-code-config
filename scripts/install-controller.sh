#!/usr/bin/env bash
# ABOUTME: Installs the sdlc controller CLI, bootstrapping uv first if needed.
# ABOUTME: Wraps `uv tool install controller/` for users without uv (Story 7.1-001).
#
# install-controller.sh — one-command install of the `sdlc` controller CLI.
#
# The controller is a Python package managed with uv (see ADR-001). This wrapper
# exists for users who do not yet have uv: it installs uv via the official
# standalone installer, then runs `uv tool install` against the `controller/`
# directory. Re-running it upgrades the installed CLI in place.
#
# Usage:
#   ./scripts/install-controller.sh            # install (or upgrade) the CLI
#   ./scripts/install-controller.sh --help     # show this help
#
# Verify afterwards with:
#   sdlc --version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTROLLER_DIR="${REPO_ROOT}/controller"

usage() {
  sed -n '6,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

log() { printf '==> %s\n' "$*"; }

if [[ ! -d "${CONTROLLER_DIR}" ]]; then
  echo "error: controller directory not found at ${CONTROLLER_DIR}" >&2
  exit 1
fi

# 1. Ensure uv is available, bootstrapping it if the user does not have it.
if ! command -v uv >/dev/null 2>&1; then
  log "uv not found; installing via the official standalone installer"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer drops uv in ~/.local/bin (or $XDG_BIN_HOME); make it visible
  # for the remainder of this script even before the user restarts their shell.
  export PATH="${HOME}/.local/bin:${XDG_BIN_HOME:-${HOME}/.local/bin}:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv installation did not put 'uv' on PATH." >&2
  echo "       Restart your shell (or source your profile) and re-run this script." >&2
  exit 1
fi

log "uv $(uv --version | awk '{print $2}') detected"

# 2. Install (or upgrade) the controller CLI as an isolated uv tool.
log "installing the sdlc controller CLI from ${CONTROLLER_DIR}"
uv tool install --force "${CONTROLLER_DIR}"

log "done. Verify with: sdlc --version"
