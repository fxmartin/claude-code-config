#!/usr/bin/env bash
# ABOUTME: Shared, sourceable helpers for hook strictness profiles, per-hook
# ABOUTME: disable lists, and SessionStart context caps (Epic-15 Story 15.2-001).
#
# Source this from any hook script so operators can tune strictness via the
# environment without editing the hook itself:
#
#   HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=hooks/hook-profile.sh
#   . "$HOOK_DIR/hook-profile.sh"
#   hook_should_run notify-telegram notification || exit 0
#
# Controls (all opt-in; leaving them unset preserves today's behavior):
#
#   SDLC_HOOK_PROFILE        minimal | standard | strict   (default: standard)
#   SDLC_DISABLED_HOOKS      space/comma list of hook names to skip
#   SDLC_SESSION_CONTEXT_MAX max characters of SessionStart-injected context
#                            (0 / unset / non-numeric = unlimited)
#
# Profiles:
#   minimal   run only essential + guardrail work; skip notification/sidebar
#   standard  today's behavior — run everything not explicitly disabled
#   strict    run everything; guardrail-class hooks cannot be disabled
#
# Every function is pure and free of side effects except hook_emit_context,
# which only writes to stdout. Safe to source under `set -euo pipefail`.

# Resolve the active profile, defaulting to "standard" and tolerating garbage.
hook_profile() {
    case "${SDLC_HOOK_PROFILE:-standard}" in
        minimal|standard|strict) printf '%s\n' "${SDLC_HOOK_PROFILE:-standard}" ;;
        *) printf 'standard\n' ;;
    esac
}

# Return 0 if hook <name> appears in $SDLC_DISABLED_HOOKS. The list is split on
# whitespace and commas, so "a,b c" and "a b c" are equivalent.
hook_in_disable_list() {
    local name="${1:-}" entry
    [ -n "$name" ] || return 1
    local raw="${SDLC_DISABLED_HOOKS:-}"
    [ -n "$raw" ] || return 1
    # Intentional word-splitting after normalizing commas to spaces.
    # shellcheck disable=SC2086
    for entry in ${raw//,/ }; do
        [ "$entry" = "$name" ] && return 0
    done
    return 1
}

# Decide whether a hook should run. Usage: hook_should_run <name> [class]
#   class in: essential | notification | sidebar | guardrail  (default: essential)
# Returns 0 to run, 1 to skip.
hook_should_run() {
    local name="${1:-}" class="${2:-essential}" profile
    profile="$(hook_profile)"

    # Strict mode keeps guardrails on no matter what — they cannot be disabled.
    if [ "$profile" = "strict" ] && [ "$class" = "guardrail" ]; then
        return 0
    fi

    # An explicit disable entry always wins (except guardrails-in-strict above).
    if hook_in_disable_list "$name"; then
        return 1
    fi

    # Minimal mode drops non-essential classes.
    if [ "$profile" = "minimal" ]; then
        case "$class" in
            notification|sidebar) return 1 ;;
        esac
    fi

    return 0
}

# Echo the SessionStart context cap in characters (0 = unlimited). A missing or
# non-numeric value is treated as unlimited so a typo never starves context.
hook_context_cap() {
    local cap="${SDLC_SESSION_CONTEXT_MAX:-0}"
    case "$cap" in
        ''|*[!0-9]*) printf '0\n' ;;
        *) printf '%s\n' "$cap" ;;
    esac
}

# Emit context, truncated to the cap. Reads from the arguments when given,
# otherwise from stdin. A cap of 0 (unset / non-numeric) passes the context
# through unchanged, preserving today's behavior.
hook_emit_context() {
    local cap text
    cap="$(hook_context_cap)"
    if [ "$#" -gt 0 ]; then
        text="$*"
    else
        text="$(cat)"
    fi
    if [ "$cap" -gt 0 ] && [ "${#text}" -gt "$cap" ]; then
        printf '%s' "${text:0:cap}"
    else
        printf '%s' "$text"
    fi
}
