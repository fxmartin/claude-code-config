#!/usr/bin/env bash
# ABOUTME: Regenerate Claude + Codex skill files from the neutral sources (Story 20.4-002).
# ABOUTME: One authored neutral source per skill emits both harness files, killing mirror drift.
#
# generate-skills.sh — transpile the harness-neutral pipeline skill sources
# (e.g. build-stories) into the Claude `SKILL.md` and the Codex mirror `SKILL.md`
# so the two runtimes stay in lock-step automatically (no hand-maintained
# copies). The body-only utility skills are mirrored separately via
# `sync-shared-skills.sh` and are not emitted here.
#
# Per ADR-003, `shared-skills/neutral/<name>.skill.md` is the single authored
# artifact; this script drives the controller's tested generator
# (`sdlc generate-skills`) to emit both targets.
#
# Subcommands:
#   generate [CLAUDE_BASE] [CODEX_BASE]
#                     Regenerate all skills. CLAUDE_BASE defaults to this repo's
#                     `plugins/autonomous-sdlc/skills`; CODEX_BASE defaults to the
#                     sibling `nix-install` mirror's skills dir (../../plugins/...).
#
# Usage:
#   ./scripts/generate-skills.sh generate [CLAUDE_BASE] [CODEX_BASE]
#   ./scripts/generate-skills.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NEUTRAL_DIR="${REPO_ROOT}/shared-skills/neutral"

# Defaults: Claude skills live in this repo; the Codex mirror lives in the
# `nix-install` parent (this repo is the `config/claude-code-config` submodule).
DEFAULT_CLAUDE_BASE="${REPO_ROOT}/plugins/autonomous-sdlc/skills"
DEFAULT_CODEX_BASE="${REPO_ROOT}/../../plugins/autonomous-sdlc/skills"

usage() {
  sed -n '4,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

cmd_generate() {
  local claude_base="${1:-${DEFAULT_CLAUDE_BASE}}"
  local codex_base="${2:-${DEFAULT_CODEX_BASE}}"
  if [[ ! -d "${NEUTRAL_DIR}" ]]; then
    echo "error: neutral skills directory not found at ${NEUTRAL_DIR}" >&2
    return 2
  fi
  # Delegate to the controller's tested generator. `sdlc` exits 0 on success,
  # 2 when the neutral dir is missing or a source fails to parse — propagate it.
  sdlc generate-skills "${NEUTRAL_DIR}" \
    --claude-base "${claude_base}" \
    --codex-base "${codex_base}"
}

main() {
  local subcommand="${1:-}"
  case "${subcommand}" in
    generate)
      shift
      cmd_generate "$@"
      ;;
    -h | --help | "")
      usage
      ;;
    *)
      echo "error: unknown subcommand '${subcommand}'" >&2
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
