#!/usr/bin/env bash
# ABOUTME: SessionStart context injector — emits a concise, secret-free project
# ABOUTME: banner as session context, capped by SDLC_SESSION_CONTEXT_MAX (15.2-001).
#
# Registered as a SessionStart hook. Its stdout is added to the session context.
# Strictness controls (see docs/hook-profiles.md, hooks/hook-profile.sh):
#   - context is *sidebar* class, so the `minimal` profile and an explicit
#     SDLC_DISABLED_HOOKS=session-context entry both make it a silent no-op;
#   - emitted context is truncated to SDLC_SESSION_CONTEXT_MAX characters
#     (unset / 0 = today's full, uncapped behavior).
# Silent no-op outside a git repo so plain shells are never touched.

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || exit 0
if [ -n "${HOOK_DIR:-}" ] && [ -f "$HOOK_DIR/hook-profile.sh" ]; then
    # shellcheck source=hooks/hook-profile.sh
    . "$HOOK_DIR/hook-profile.sh"
fi

# Sidebar/context work is non-essential — honor profile + disable-list when the
# helper is available; degrade to "always emit" if it could not be sourced.
if command -v hook_should_run >/dev/null 2>&1; then
    hook_should_run session-context sidebar || exit 0
fi

# Only inject context inside a git repo; a plain directory is left untouched.
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$repo_root" ] || exit 0

# Build a short, secret-free banner. Strip any worktree suffix so sub-agents
# report the real project, not the throwaway worktree path.
project="$(basename "${repo_root%/.claude/worktrees/*}")"
branch="$(git -C "$repo_root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
context="SDLC session — project: ${project}, branch: ${branch}."

# Surface the ledger's presence (not its contents) as a self-service nudge.
if [ -f "$repo_root/.sdlc-state.db" ]; then
    context="${context} Ledger present; run 'sdlc status' or 'sdlc doctor' for detail."
fi

# Emit through the cap. Unset / 0 / non-numeric cap = full passthrough.
if command -v hook_emit_context >/dev/null 2>&1; then
    printf '%s' "$context" | hook_emit_context
else
    printf '%s' "$context"
fi
exit 0
