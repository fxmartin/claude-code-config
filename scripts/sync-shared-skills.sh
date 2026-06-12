#!/usr/bin/env bash
# ABOUTME: Drives the Codex mirror sync of the shared skill set (Story 7.4-001).
# ABOUTME: Consumers run `update`; either side runs `verify` to confirm parity.
#
# sync-shared-skills.sh — keep the shared skill set in lockstep across repos.
#
# Per ADR-002, `claude-code-config` (this repo) hosts the shared skill set under
# `shared-skills/`, and the `nix-install` Codex mirror consumes it as a git
# submodule. There is exactly one copy of each shared skill, so the two runtimes
# cannot drift.
#
# Subcommands:
#   update            Pull the latest shared skills (consumer side). Wraps the
#                     single documented command: `git submodule update --remote`.
#   verify <src> <c>  Confirm a consumer checkout mirrors the source byte-for-byte
#                     by delegating to `sdlc sync-check` (the controller's hermetic
#                     parity check). <src> and <c> are shared-skills directories.
#   list              Print the shared skill names this source repo publishes.
#
# Usage:
#   ./scripts/sync-shared-skills.sh update [SUBMODULE_PATH]
#   ./scripts/sync-shared-skills.sh verify SOURCE_DIR CONSUMER_DIR
#   ./scripts/sync-shared-skills.sh list
#   ./scripts/sync-shared-skills.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARED_DIR="${REPO_ROOT}/shared-skills"

usage() {
  sed -n '4,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

cmd_update() {
  # The one documented consumer command. An optional submodule path narrows the
  # update to just the shared-skills submodule on the consumer side.
  local submodule_path="${1:-}"
  if [[ -n "${submodule_path}" ]]; then
    git submodule update --remote "${submodule_path}"
  else
    git submodule update --remote
  fi
  echo "shared skills updated — run 'verify' to confirm parity."
}

cmd_verify() {
  local source_dir="${1:-}"
  local consumer_dir="${2:-}"
  if [[ -z "${source_dir}" || -z "${consumer_dir}" ]]; then
    echo "error: verify requires SOURCE_DIR and CONSUMER_DIR" >&2
    return 2
  fi
  # Delegate to the controller's tested parity check. `sdlc` exits 0 in sync,
  # 1 on drift, 2 on a missing directory — propagate that verbatim.
  sdlc sync-check "${source_dir}" "${consumer_dir}"
}

cmd_list() {
  if [[ ! -d "${SHARED_DIR}" ]]; then
    echo "error: shared-skills directory not found at ${SHARED_DIR}" >&2
    return 2
  fi
  # One skill per `*.md` file, README excluded — mirrors sync.discover_shared_skills.
  find "${SHARED_DIR}" -maxdepth 1 -type f -name '*.md' ! -name 'README.md' \
    -exec basename {} .md \; | sort
}

main() {
  local subcommand="${1:-}"
  case "${subcommand}" in
    update)
      shift
      cmd_update "$@"
      ;;
    verify)
      shift
      cmd_verify "$@"
      ;;
    list)
      cmd_list
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
