#!/usr/bin/env bash
# ABOUTME: Claude Code Notification/Stop hook that pings Telegram via notify-telegram.sh.
# ABOUTME: Throttled, quiet on short turns and fleet agents; always exits 0, no stdout.

# Deliberately no `set -e`: a notification hook must never fail the session.

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
STATE_DIR="${HOME}/.claude/state"
THROTTLE_SECONDS=60

# Mask common token shapes before a message leaves the host.
_mask_tokens() {
    sed -E \
        -e 's/[0-9]{6,}:[A-Za-z0-9_-]{30,}/[redacted]/g' \
        -e 's/(sk|pk)-[A-Za-z0-9_-]{16,}/[redacted]/g' \
        -e 's/(ghp|gho|ghs|ghu|ghr|github_pat|glpat)[-_][A-Za-z0-9_-]{16,}/[redacted]/g' \
        -e 's/[Bb]earer +[A-Za-z0-9._~+\/=-]{16,}/Bearer [redacted]/g'
}

# Seconds since the last real user prompt in the transcript; empty if unknown.
_turn_seconds() {
    local transcript="$1" ts start now
    [ -r "$transcript" ] || return 0
    ts="$(tail -n 2000 "$transcript" 2>/dev/null | jq -Rr '
        fromjson? | select(.type == "user" and (.timestamp | type) == "string")
        | select((.message.content | type) == "string"
                 or ((.message.content | type) == "array"
                     and any(.message.content[]; .type == "text")))
        | .timestamp' 2>/dev/null | tail -n 1)"
    [ -n "$ts" ] || return 0
    start="$(jq -nr --arg t "$ts" '$t | sub("\\.[0-9]+"; "") | fromdateiso8601' 2>/dev/null)" || return 0
    case "$start" in ''|*[!0-9]*) return 0 ;; esac
    now="$(date +%s)"
    echo $((now - start))
}

# Return 0 if a ping for this session+event is allowed; records the attempt.
_throttle_ok() {
    local sid="$1" event="$2" stamp last now
    sid="${sid//[^A-Za-z0-9_-]/_}"
    stamp="$STATE_DIR/notify-session-event-${sid:-unknown}-${event}"
    now="$(date +%s)"
    last="$(cat "$stamp" 2>/dev/null)"
    case "$last" in *[!0-9]*) last="" ;; esac
    if [ -n "$last" ] && [ $((now - last)) -lt "$THROTTLE_SECONDS" ]; then
        return 1
    fi
    mkdir -p "$STATE_DIR" 2>/dev/null && echo "$now" > "$stamp" 2>/dev/null
    return 0
}

_main() {
    [ -z "${SDLC_BATCH_BUILD:-}" ] || return 0
    [ "${CLAUDE_NOTIFY_SESSION:-}" != "0" ] || return 0
    command -v jq >/dev/null 2>&1 || return 0

    # shellcheck source=hooks/hook-profile.sh
    # shellcheck disable=SC1091
    [ -f "$HOOK_DIR/hook-profile.sh" ] && . "$HOOK_DIR/hook-profile.sh"
    if command -v hook_should_run >/dev/null 2>&1; then
        hook_should_run notify-session-event notification || return 0
    fi

    local input event sid active title body host tmux_name="" message transcript secs min
    input="$(cat)"
    [ -n "$input" ] || return 0
    jq -e . >/dev/null 2>&1 <<<"$input" || return 0

    active="$(jq -r '.stop_hook_active // false' <<<"$input")"
    [ "$active" != "true" ] || return 0

    event="$(jq -r '.hook_event_name // empty' <<<"$input")"
    sid="$(jq -r '.session_id // empty' <<<"$input")"
    host="$(hostname -s 2>/dev/null || hostname 2>/dev/null)"
    if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
        tmux_name="$(tmux display-message -p '#S' 2>/dev/null)"
    fi
    body="$host"
    [ -z "$tmux_name" ] || body="$body · tmux $tmux_name"

    case "$event" in
        Notification)
            title="Waiting for you"
            message="$(jq -r '.message // empty' <<<"$input" | _mask_tokens)"
            message="${message:0:200}"
            [ -z "$message" ] || body="$body"$'\n'"$message"
            ;;
        Stop)
            title="Finished"
            transcript="$(jq -r '.transcript_path // empty' <<<"$input")"
            [ -n "$transcript" ] || return 0
            secs="$(_turn_seconds "$transcript")"
            [ -n "$secs" ] || return 0
            min="${CLAUDE_NOTIFY_MIN_TURN_SECONDS:-120}"
            case "$min" in ''|*[!0-9]*) min=120 ;; esac
            [ "$secs" -ge "$min" ] || return 0
            ;;
        *) return 0 ;;
    esac

    _throttle_ok "$sid" "$event" || return 0
    [ -x "$HOOK_DIR/notify-telegram.sh" ] || return 0
    if command -v timeout >/dev/null 2>&1; then
        timeout 8 "$HOOK_DIR/notify-telegram.sh" "$title" "$body" >/dev/null 2>&1
    else
        "$HOOK_DIR/notify-telegram.sh" "$title" "$body" >/dev/null 2>&1
    fi
}

_main || true
exit 0
