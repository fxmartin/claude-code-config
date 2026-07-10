#!/usr/bin/env bash
# ABOUTME: One-command deploy — installs the sdlc controller CLI and moves the
# ABOUTME: autonomous-sdlc plugin pointer to the same version, so they can't drift.
#
# deploy.sh — deploy this repo's two installable artifacts together.
#
# The controller CLI and the Claude Code plugin ship from the same repo and the
# same version number, but they install through completely separate mechanisms:
#
#   controller → `uv tool install --force controller/`   (scripts/install-controller.sh)
#   plugin     → `claude plugin update <plugin>@<marketplace>`
#
# Running only the first leaves the plugin's skills on whatever version they were
# last explicitly updated to, driving a controller they no longer match. That
# drift is silent — `git pull` moves neither pointer. This script runs both.
#
# Usage:
#   ./scripts/deploy.sh                  # controller + plugin
#   ./scripts/deploy.sh --controller-only  # no Claude Code on this box
#   ./scripts/deploy.sh --plugin-only
#   ./scripts/deploy.sh --dry-run        # print what would run, change nothing
#   ./scripts/deploy.sh --help
#
# A default run REQUIRES `claude` on PATH. It is checked in preflight, before
# anything is installed, so a box without Claude Code aborts with the machine
# untouched rather than half-deployed: moving only one of the two pointers is the
# drift this script exists to prevent. Use --controller-only to deploy the
# controller alone, deliberately.
#
# The plugin step needs a restart of Claude Code to take effect; the controller
# step takes effect immediately.
#
# Verify afterwards with:
#   sdlc --version && claude plugin list

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The plugin this repo publishes, as `plugin@marketplace`. Both halves are
# declared in .claude-plugin/marketplace.json.
PLUGIN_ID="autonomous-sdlc@fx-claude-config"

# Test seam: tests/deploy.bats points this at a stub so the suite never runs a
# real `uv tool install`. Defaults to the real installer.
INSTALL_CONTROLLER="${INSTALL_CONTROLLER:-${SCRIPT_DIR}/install-controller.sh}"

DO_CONTROLLER=true
DO_PLUGIN=true
DRY_RUN=false

usage() {
  sed -n '6,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)         usage; exit 0 ;;
    --dry-run)         DRY_RUN=true ;;
    --controller-only) DO_PLUGIN=false ;;
    --plugin-only)     DO_CONTROLLER=false ;;
    *)                 die "unknown flag: $1 (try --help)" ;;
  esac
  shift
done

if [[ "${DO_CONTROLLER}" == false && "${DO_PLUGIN}" == false ]]; then
  die "--controller-only and --plugin-only are mutually exclusive"
fi

# Preflight — validate every precondition BEFORE mutating anything.
#
# The two steps move independent pointers, and step 1 cannot be rolled back once
# `uv tool install --force` has recreated the venv. So a step-2 precondition that
# fails after step 1 succeeded leaves the controller newer than the plugin: the
# drift this script exists to prevent, merely with a non-zero exit describing it.
# Both preconditions are knowable up front, so we check them up front and abort
# with the machine untouched.
#
# Skipped under --dry-run, which mutates nothing and so cannot drift.
if [[ "${DRY_RUN}" == false ]]; then
  if [[ "${DO_CONTROLLER}" == true && ! -x "${INSTALL_CONTROLLER}" ]]; then
    die "controller installer not found or not executable: ${INSTALL_CONTROLLER}"
  fi
  if [[ "${DO_PLUGIN}" == true ]] && ! command -v claude >/dev/null 2>&1; then
    die "claude not found on PATH; cannot update ${PLUGIN_ID}.
       Nothing was changed. Install Claude Code and re-run, or pass
       --controller-only to deploy the controller alone and accept that the
       plugin stays on its current version."
  fi
fi

# 1. Controller CLI. install-controller.sh bootstraps uv when absent and is
#    itself idempotent, so re-running deploy.sh is safe.
if [[ "${DO_CONTROLLER}" == true ]]; then
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: ${INSTALL_CONTROLLER}"
  else
    log "installing the sdlc controller CLI"
    "${INSTALL_CONTROLLER}"
  fi
fi

# 2. Plugin pointer. `claude` was proven present in preflight.
if [[ "${DO_PLUGIN}" == true ]]; then
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: claude plugin update ${PLUGIN_ID}"
  else
    log "updating plugin ${PLUGIN_ID}"
    claude plugin update "${PLUGIN_ID}"
    log "plugin updated — restart Claude Code to load it"
  fi
fi

if [[ "${DRY_RUN}" == true ]]; then
  log "dry run complete; nothing was changed"
else
  log "done. Verify with: sdlc --version && claude plugin list"
fi
