#!/usr/bin/env bash
# sdlc-state-emit.sh — single-writer wrapper around scripts/sdlc-state.sh.
#
# Used by the build-stories orchestrator and every dispatched sub-agent to
# write run / story / stage / event records to the SQLite ledger without
# embedding raw SQL in agent prompts or learning the CLI's flag layout.
#
# Story 4.2-001 (Epic-04): agents call this hook at the same hook points
# where they emit `cmux-bridge log` notifications, so the ledger and the
# sidebar timeline stay in lockstep.
#
# Contract:
#
#   sdlc-state-emit.sh <subcommand> [args...]
#
# `subcommand` is forwarded as-is to `sdlc-state.sh`. Two subcommands print
# something on stdout that the caller should capture:
#
#   run_id=$(sdlc-state-emit.sh run-create epic-04 parallel)
#
# Database resolution (in priority order):
#
#   1. `$SDLC_STATE_DB` (explicit override, used by tests and orchestrators
#      that pin a per-run DB).
#   2. `$REPO_ROOT/.sdlc-state.db` if we are inside a git repo.
#   3. `./.sdlc-state.db` (cwd).
#
# Graceful degradation:
#
#   If neither $SDLC_STATE_DB nor `git rev-parse` resolves a writable DB
#   path AND no `.sdlc-state.db` exists in cwd, the hook EXITS 0 silently.
#   This matches the cmux-bridge.sh pattern: agents that run outside a
#   ledger-enabled session must not crash on a missing DB.
#
#   The single exception is when `$SDLC_STATE_DB` is explicitly set — then
#   we honor it even if it does not yet exist (init may follow).

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate sdlc-state.sh. The hook script may be invoked either from the
# dev-clone (./hooks/sdlc-state-emit.sh, sibling to ./scripts/) or from the
# install-time symlink at ~/.claude/hooks/sdlc-state-emit.sh, in which case
# BASH_SOURCE still resolves to the real file in the repo via realpath.
# ---------------------------------------------------------------------------

_resolve_state_script() {
    local self
    self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    # If invoked through a symlink, follow it so we land next to scripts/.
    if [ -L "${self}" ]; then
        # readlink -f is GNU; macOS BSD readlink lacks -f, so use the
        # python fallback only if needed.
        if command -v greadlink >/dev/null 2>&1; then
            self="$(greadlink -f "${self}")"
        elif readlink -f /dev/null >/dev/null 2>&1; then
            self="$(readlink -f "${self}")"
        else
            # Manual one-hop deref — sufficient for our install layout
            # which only ever uses single-level symlinks.
            local target
            target="$(readlink "${self}")"
            case "${target}" in
                /*) self="${target}" ;;
                *)  self="$(cd "$(dirname "${self}")" && cd "$(dirname "${target}")" && pwd)/$(basename "${target}")" ;;
            esac
        fi
    fi
    local hooks_dir
    hooks_dir="$(cd "$(dirname "${self}")" && pwd)"
    local repo_root
    repo_root="$(cd "${hooks_dir}/.." && pwd)"
    echo "${repo_root}/scripts/sdlc-state.sh"
}

_resolve_db_path() {
    # Priority 1: explicit override.
    if [ -n "${SDLC_STATE_DB:-}" ]; then
        echo "${SDLC_STATE_DB}"
        return 0
    fi
    # Priority 2: repo-root .sdlc-state.db (only if it exists — we do not
    # auto-create a ledger here).
    local toplevel
    if toplevel="$(git rev-parse --show-toplevel 2>/dev/null)"; then
        if [ -f "${toplevel}/.sdlc-state.db" ]; then
            echo "${toplevel}/.sdlc-state.db"
            return 0
        fi
    fi
    # Priority 3: cwd, if a ledger already lives here.
    if [ -f "./.sdlc-state.db" ]; then
        echo "./.sdlc-state.db"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

if [ $# -lt 1 ]; then
    cat >&2 <<'EOF'
Usage: sdlc-state-emit.sh <subcommand> [args...]

Forwards the call to scripts/sdlc-state.sh with the resolved DB path.

Environment:
  SDLC_STATE_DB   Explicit DB path (highest priority).
  SDLC_RUN_ID     Run identifier (exported by the orchestrator, inherited
                  by sub-agents — informational only here).

Subcommands:
  See `scripts/sdlc-state.sh --help` for the full write-path API.

Exits 0 silently when no ledger DB can be located (graceful degradation,
mirrors cmux-bridge.sh behavior).
EOF
    exit 1
fi

if ! db_path="$(_resolve_db_path)"; then
    # No ledger configured — quietly succeed so agents that emit ledger
    # calls in environments without a ledger do not fail the build.
    exit 0
fi

state_script="$(_resolve_state_script)"
if [ ! -x "${state_script}" ]; then
    # The installer should always link scripts/sdlc-state.sh as executable;
    # if it is missing the hook fails loudly so the issue is visible.
    echo "sdlc-state-emit: cannot find sdlc-state.sh at ${state_script}" >&2
    exit 1
fi

exec "${state_script}" --db "${db_path}" "$@"
