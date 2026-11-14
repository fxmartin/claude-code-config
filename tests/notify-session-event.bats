#!/usr/bin/env bats
# ABOUTME: Tests for notify-session-event.sh — Telegram pings on Notification/Stop events.
# ABOUTME: Uses a fake sibling notify-telegram.sh on a temp HOME; no network involved.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    FAKE_HOME="$(mktemp -d)"
    # Copy the hook next to a fake notifier so the sibling lookup hits the fake.
    HOOK_DIR="$FAKE_HOME/hooks"
    mkdir -p "$HOOK_DIR"
    cp "$REPO_ROOT/hooks/notify-session-event.sh" "$REPO_ROOT/hooks/hook-profile.sh" "$HOOK_DIR/"
    SENT="$FAKE_HOME/sent.log"
    cat > "$HOOK_DIR/notify-telegram.sh" <<FAKE
#!/bin/bash
printf '%s|%s\n' "\$1" "\$(printf %s "\$2" | tr '\\n' ' ')" >> "$SENT"
FAKE
    chmod +x "$HOOK_DIR/notify-telegram.sh"
    HOOK="$HOOK_DIR/notify-session-event.sh"
    NOW="$(date +%s)"
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "$FAKE_HOME"
}

# run_hook <stdin-json> [VAR=value ...]
run_hook() {
    local payload="$1"; shift
    run env -u SDLC_BATCH_BUILD -u SDLC_HOOK_PROFILE -u SDLC_DISABLED_HOOKS \
        -u CLAUDE_NOTIFY_SESSION -u CLAUDE_NOTIFY_MIN_TURN_SECONDS -u TMUX \
        HOME="$FAKE_HOME" "$@" bash "$HOOK" <<<"$payload"
}

sent_count() { [ -f "$SENT" ] && wc -l < "$SENT" | tr -d ' ' || echo 0; }

# transcript_with_prompt <seconds-ago>: last user prompt that long ago.
transcript_with_prompt() {
    local ts
    ts="$(jq -nr "($NOW - $1) | todate")"
    local f="$FAKE_HOME/transcript.jsonl"
    printf '%s\n' \
        "{\"type\":\"user\",\"timestamp\":\"$(jq -nr "($NOW - 9999)|todate")\",\"message\":{\"content\":\"old\"}}" \
        "{\"type\":\"user\",\"timestamp\":\"$ts\",\"message\":{\"content\":\"hello\"}}" \
        "{\"type\":\"assistant\",\"timestamp\":\"$(jq -nr "($NOW - 1)|todate")\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}}" \
        "{\"type\":\"user\",\"timestamp\":\"$(jq -nr "($NOW - 1)|todate")\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"content\":\"x\"}]}}" \
        > "$f"
    echo "$f"
}

notif() { jq -nc --arg m "$1" '{hook_event_name:"Notification",session_id:"s1",message:$m}'; }
stop() { jq -nc --arg t "$1" '{hook_event_name:"Stop",session_id:"s1",transcript_path:$t}'; }

@test "Notification sends one ping with host and message; second within 60s is throttled" {
    run_hook "$(notif 'Claude needs your permission')"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ "$(sent_count)" -eq 1 ]
    grep -q '^Waiting for you|' "$SENT"
    grep -q "$(hostname -s 2>/dev/null || hostname)" "$SENT"
    grep -q 'Claude needs your permission' "$SENT"
    run_hook "$(notif 'again')"
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 1 ]
}

@test "throttle is per session" {
    run_hook "$(notif 'one')"
    run_hook "$(notif 'two' | jq -c '.session_id="s2"')"
    [ "$(sent_count)" -eq 2 ]
}

@test "Notification message is truncated to 200 characters" {
    long="$(printf 'a%.0s' $(seq 1 300))"
    run_hook "$(notif "$long")"
    [ "$(sent_count)" -eq 1 ]
    ! grep -q 'a\{201\}' "$SENT"
    grep -q 'a\{200\}' "$SENT"
}

@test "Notification message has tokens masked" {
    run_hook "$(notif 'leak ghp_abcdefghijklmnopqrstuvwxyz0123456789 and 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')"
    [ "$(sent_count)" -eq 1 ]
    ! grep -q 'ghp_abcdef' "$SENT"
    ! grep -q 'AAAAAAAAAA' "$SENT"
}

@test "Stop after a 10-second turn sends nothing" {
    t="$(transcript_with_prompt 10)"
    run_hook "$(stop "$t")"
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "Stop after a 5-minute turn sends one Finished ping" {
    t="$(transcript_with_prompt 300)"
    run_hook "$(stop "$t")"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ "$(sent_count)" -eq 1 ]
    grep -q '^Finished|' "$SENT"
}

@test "CLAUDE_NOTIFY_MIN_TURN_SECONDS overrides the threshold" {
    t="$(transcript_with_prompt 10)"
    run_hook "$(stop "$t")" CLAUDE_NOTIFY_MIN_TURN_SECONDS=5
    [ "$(sent_count)" -eq 1 ]
}

@test "Stop with unknown duration sends nothing" {
    run_hook "$(stop "$FAKE_HOME/missing.jsonl")"
    [ "$status" -eq 0 ]
    run_hook "$(jq -nc '{hook_event_name:"Stop",session_id:"s1"}')"
    [ "$status" -eq 0 ]
    echo 'not json' > "$FAKE_HOME/bad.jsonl"
    run_hook "$(stop "$FAKE_HOME/bad.jsonl")"
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "SDLC_BATCH_BUILD suppresses pings" {
    run_hook "$(notif x)" SDLC_BATCH_BUILD=1
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "stop_hook_active suppresses pings" {
    t="$(transcript_with_prompt 300)"
    run_hook "$(stop "$t" | jq -c '.stop_hook_active=true')"
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "CLAUDE_NOTIFY_SESSION=0 suppresses pings" {
    run_hook "$(notif x)" CLAUDE_NOTIFY_SESSION=0
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "minimal profile suppresses pings" {
    run_hook "$(notif x)" SDLC_HOOK_PROFILE=minimal
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "SDLC_DISABLED_HOOKS entry suppresses pings" {
    run_hook "$(notif x)" SDLC_DISABLED_HOOKS=notify-session-event
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "empty and malformed stdin exit 0 silently without sending" {
    run_hook ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    run_hook "{not json"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ "$(sent_count)" -eq 0 ]
}

@test "missing notifier does not fail the session" {
    rm "$HOOK_DIR/notify-telegram.sh"
    run_hook "$(notif x)"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "settings.template.json wires Notification and Stop, keeping worktree-gc" {
    f="$REPO_ROOT/settings.template.json"
    jq -e . "$f" >/dev/null
    jq -e '[.hooks.Stop[].hooks[].command] | any(endswith("worktree-gc.sh")) and any(endswith("notify-session-event.sh"))' "$f"
    jq -e '[.hooks.Notification[].hooks[].command] | any(endswith("notify-session-event.sh"))' "$f"
    jq -e '.hooks.SessionStart and .hooks.PostToolUse' "$f" >/dev/null
}

@test "installer merge adds the hook to an old settings.json idempotently" {
    work="$(mktemp -d)"
    jq -c 'del(.hooks.Notification) | .hooks.Stop[0].hooks |= map(select(.command | endswith("notify-session-event.sh") | not))' \
        "$REPO_ROOT/settings.template.json" > "$work/settings.json"
    run bash -c "SCRIPT_DIR='$work'; info(){ :; }; warn(){ :; }; $(sed -n '/^register_session_notify_hook()/,/^}/p' "$REPO_ROOT/install/core.sh"); register_session_notify_hook; register_session_notify_hook"
    [ "$status" -eq 0 ]
    jq -e '[.hooks.Notification[].hooks[].command] | length == 1' "$work/settings.json"
    jq -e '[.hooks.Stop[].hooks[].command] | map(select(endswith("notify-session-event.sh"))) | length == 1' "$work/settings.json"
    jq -e '[.hooks.Stop[].hooks[].command] | any(endswith("worktree-gc.sh"))' "$work/settings.json"
    rm -rf "$work"
}

@test "unknown hook event sends nothing" {
    run_hook '{"hook_event_name":"SessionStart","session_id":"s1"}'
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "Stop without transcript_path sends nothing" {
    run_hook '{"hook_event_name":"Stop","session_id":"s1"}'
    [ "$status" -eq 0 ]
    [ "$(sent_count)" -eq 0 ]
}

@test "non-numeric CLAUDE_NOTIFY_MIN_TURN_SECONDS falls back to 120s" {
    run_hook "$(stop "$(transcript_with_prompt 10)")" CLAUDE_NOTIFY_MIN_TURN_SECONDS=abc
    [ "$(sent_count)" -eq 0 ]
    run_hook "$(stop "$(transcript_with_prompt 300)")" CLAUDE_NOTIFY_MIN_TURN_SECONDS=abc
    [ "$(sent_count)" -eq 1 ]
}

@test "Notification without message still pings with host only" {
    run_hook '{"hook_event_name":"Notification","session_id":"s1"}'
    [ "$(sent_count)" -eq 1 ]
    grep -q '^Waiting for you|' "$SENT"
}

@test "tmux session name is included when TMUX is set" {
    command -v tmux >/dev/null 2>&1 || skip "tmux not installed"
    mkdir -p "$FAKE_HOME/bin"
    printf '#!/bin/sh\necho mysess\n' > "$FAKE_HOME/bin/tmux"
    chmod +x "$FAKE_HOME/bin/tmux"
    run_hook "$(notif hi)" TMUX=/tmp/x,1,0 PATH="$FAKE_HOME/bin:$PATH"
    grep -q 'tmux mysess' "$SENT"
}

@test "installer merge: no settings.json is a no-op" {
    work="$(mktemp -d)"
    run bash -c "SCRIPT_DIR='$work'; info(){ :; }; warn(){ :; }; $(sed -n '/^register_session_notify_hook()/,/^}/p' "$REPO_ROOT/install/core.sh"); register_session_notify_hook"
    [ "$status" -eq 0 ]
    [ ! -e "$work/settings.json" ]
    rm -rf "$work"
}

@test "installer merge: dry-run leaves settings.json untouched" {
    work="$(mktemp -d)"
    echo '{"hooks":{}}' > "$work/settings.json"
    run bash -c "SCRIPT_DIR='$work'; DRY_RUN=true; info(){ :; }; warn(){ :; }; $(sed -n '/^register_session_notify_hook()/,/^}/p' "$REPO_ROOT/install/core.sh"); register_session_notify_hook"
    [ "$status" -eq 0 ]
    [[ "$output" == *dry-run* ]]
    [ "$(cat "$work/settings.json")" = '{"hooks":{}}' ]
    rm -rf "$work"
}
