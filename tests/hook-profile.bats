#!/usr/bin/env bats
# ABOUTME: Tests for hooks/hook-profile.sh — hook strictness profiles, per-hook
# ABOUTME: disable lists, and SessionStart context caps (Epic-15 Story 15.2-001).

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    LIB="$REPO_ROOT/hooks/hook-profile.sh"
    NOTIFY="$REPO_ROOT/hooks/notify-telegram.sh"
    FAKE_HOME="$(mktemp -d)"
    # Source the library into the test shell so functions are callable directly.
    # shellcheck source=/dev/null
    source "$LIB"
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "$FAKE_HOME"
}

# --- hook_profile -----------------------------------------------------------

@test "profile defaults to standard when unset" {
    run env -u SDLC_HOOK_PROFILE bash -c "source '$LIB'; hook_profile"
    [ "$status" -eq 0 ]
    [ "$output" = "standard" ]
}

@test "valid profiles are honored verbatim" {
    for p in minimal standard strict; do
        run env SDLC_HOOK_PROFILE="$p" bash -c "source '$LIB'; hook_profile"
        [ "$status" -eq 0 ]
        [ "$output" = "$p" ]
    done
}

@test "an invalid profile falls back to standard" {
    run env SDLC_HOOK_PROFILE="bananas" bash -c "source '$LIB'; hook_profile"
    [ "$status" -eq 0 ]
    [ "$output" = "standard" ]
}

# --- hook_in_disable_list ---------------------------------------------------

@test "disable list matches a named hook (space-separated)" {
    SDLC_DISABLED_HOOKS="alpha notify-telegram beta" run_in_disable notify-telegram 0
}

@test "disable list matches a named hook (comma-separated)" {
    SDLC_DISABLED_HOOKS="alpha,notify-telegram,beta" run_in_disable notify-telegram 0
}

@test "disable list does not match an absent hook" {
    SDLC_DISABLED_HOOKS="alpha beta" run_in_disable notify-telegram 1
}

@test "empty disable list matches nothing" {
    run env -u SDLC_DISABLED_HOOKS bash -c "source '$LIB'; hook_in_disable_list notify-telegram"
    [ "$status" -eq 1 ]
}

# Helper: assert hook_in_disable_list <name> exits with <expected status>.
run_in_disable() {
    local name="$1" expected="$2"
    run env SDLC_DISABLED_HOOKS="$SDLC_DISABLED_HOOKS" \
        bash -c "source '$LIB'; hook_in_disable_list '$name'"
    [ "$status" -eq "$expected" ]
}

# --- hook_should_run --------------------------------------------------------

@test "standard profile runs every class when nothing is disabled" {
    for class in essential notification sidebar guardrail; do
        run env -u SDLC_HOOK_PROFILE -u SDLC_DISABLED_HOOKS \
            bash -c "source '$LIB'; hook_should_run somehook $class"
        [ "$status" -eq 0 ]
    done
}

@test "minimal profile skips notification and sidebar but keeps essential/guardrail" {
    run env SDLC_HOOK_PROFILE=minimal bash -c "source '$LIB'; hook_should_run h notification"
    [ "$status" -eq 1 ]
    run env SDLC_HOOK_PROFILE=minimal bash -c "source '$LIB'; hook_should_run h sidebar"
    [ "$status" -eq 1 ]
    run env SDLC_HOOK_PROFILE=minimal bash -c "source '$LIB'; hook_should_run h essential"
    [ "$status" -eq 0 ]
    run env SDLC_HOOK_PROFILE=minimal bash -c "source '$LIB'; hook_should_run h guardrail"
    [ "$status" -eq 0 ]
}

@test "disable list skips a hook regardless of class under standard" {
    run env SDLC_DISABLED_HOOKS="h" bash -c "source '$LIB'; hook_should_run h essential"
    [ "$status" -eq 1 ]
}

@test "strict profile keeps guardrails on even when disable-listed" {
    run env SDLC_HOOK_PROFILE=strict SDLC_DISABLED_HOOKS="h" \
        bash -c "source '$LIB'; hook_should_run h guardrail"
    [ "$status" -eq 0 ]
    # ...but a non-guardrail can still be disabled under strict.
    run env SDLC_HOOK_PROFILE=strict SDLC_DISABLED_HOOKS="h" \
        bash -c "source '$LIB'; hook_should_run h notification"
    [ "$status" -eq 1 ]
}

# --- context cap ------------------------------------------------------------

@test "context cap is 0 (unlimited) when unset" {
    run env -u SDLC_SESSION_CONTEXT_MAX bash -c "source '$LIB'; hook_context_cap"
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
}

@test "non-numeric context cap is treated as unlimited" {
    run env SDLC_SESSION_CONTEXT_MAX="lots" bash -c "source '$LIB'; hook_context_cap"
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
}

@test "unset cap passes context through unchanged" {
    run env -u SDLC_SESSION_CONTEXT_MAX \
        bash -c "source '$LIB'; printf '%s' abcdefghij | hook_emit_context"
    [ "$status" -eq 0 ]
    [ "$output" = "abcdefghij" ]
}

@test "context longer than the cap is truncated to the cap" {
    run env SDLC_SESSION_CONTEXT_MAX=5 \
        bash -c "source '$LIB'; printf '%s' abcdefghij | hook_emit_context"
    [ "$status" -eq 0 ]
    [ "$output" = "abcde" ]
}

@test "context shorter than the cap is left intact" {
    run env SDLC_SESSION_CONTEXT_MAX=50 \
        bash -c "source '$LIB'; printf '%s' short | hook_emit_context"
    [ "$status" -eq 0 ]
    [ "$output" = "short" ]
}

@test "context can be passed as arguments instead of stdin" {
    run env SDLC_SESSION_CONTEXT_MAX=4 \
        bash -c "source '$LIB'; hook_emit_context abcdefgh"
    [ "$status" -eq 0 ]
    [ "$output" = "abcd" ]
}

# --- integration: notify-telegram honors the controls -----------------------

@test "notify-telegram runs under the default (unset) profile" {
    run env -u SDLC_HOOK_PROFILE -u SDLC_DISABLED_HOOKS \
        HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        TELEGRAM_BOT_TOKEN="tok-default" TELEGRAM_CHAT_ID="1" \
        bash "$NOTIFY" "Title" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"tok-default"* ]]
}

@test "notify-telegram is skipped under the minimal profile" {
    run env HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 SDLC_HOOK_PROFILE=minimal \
        TELEGRAM_BOT_TOKEN="tok-min" TELEGRAM_CHAT_ID="1" \
        bash "$NOTIFY" "Title" "Body"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "notify-telegram is skipped when on the disable list" {
    run env HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 SDLC_DISABLED_HOOKS="notify-telegram" \
        TELEGRAM_BOT_TOKEN="tok-dis" TELEGRAM_CHAT_ID="1" \
        bash "$NOTIFY" "Title" "Body"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "notify-telegram still runs under strict (notifications are not guardrails-only)" {
    run env HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 SDLC_HOOK_PROFILE=strict \
        TELEGRAM_BOT_TOKEN="tok-strict" TELEGRAM_CHAT_ID="1" \
        bash "$NOTIFY" "Title" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"tok-strict"* ]]
}

# --- integration: session-context wires the cap end-to-end -------------------

CTX="$BATS_TEST_DIRNAME/../hooks/session-context.sh"

# Stand up a throwaway git repo so the SessionStart hook has a real toplevel.
_mk_repo() {
    REPO="$(mktemp -d)/repo"
    mkdir -p "$REPO"
    git -C "$REPO" init -q -b main
    git -C "$REPO" config user.email t@t.t
    git -C "$REPO" config user.name t
    echo seed > "$REPO/seed"
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m seed
}

@test "session-context injects a banner inside a git repo by default" {
    _mk_repo
    run env -u SDLC_SESSION_CONTEXT_MAX -u SDLC_HOOK_PROFILE -u SDLC_DISABLED_HOOKS \
        bash -c "cd '$REPO' && bash '$CTX'"
    [ "$status" -eq 0 ]
    [[ "$output" == *"SDLC session"* ]]
    [[ "$output" == *"branch: main"* ]]
    rm -rf "$(dirname "$REPO")"
}

@test "session-context output is truncated to the context cap" {
    _mk_repo
    run env SDLC_SESSION_CONTEXT_MAX=8 bash -c "cd '$REPO' && bash '$CTX'"
    [ "$status" -eq 0 ]
    [ "${#output}" -eq 8 ]
    [ "$output" = "SDLC ses" ]
    rm -rf "$(dirname "$REPO")"
}

@test "session-context is a silent no-op under the minimal profile" {
    _mk_repo
    run env SDLC_HOOK_PROFILE=minimal bash -c "cd '$REPO' && bash '$CTX'"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$(dirname "$REPO")"
}

@test "session-context is a silent no-op when on the disable list" {
    _mk_repo
    run env SDLC_DISABLED_HOOKS="session-context" bash -c "cd '$REPO' && bash '$CTX'"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$(dirname "$REPO")"
}

@test "session-context is a silent no-op outside a git repo" {
    PLAIN="$(mktemp -d)"
    run env -u SDLC_SESSION_CONTEXT_MAX bash -c "cd '$PLAIN' && bash '$CTX'"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$PLAIN"
}
