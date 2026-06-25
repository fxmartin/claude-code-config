#!/bin/bash
# ABOUTME: Standalone Telegram notifier for autonomous skills (no terminal deps).
# ABOUTME: Usage: notify-telegram.sh <title> <body>. Silent no-op when unconfigured.

set -euo pipefail

# Story 15.2-001: honor hook strictness controls. A Telegram ping is a
# non-essential *notification*, so the `minimal` profile and an explicit
# SDLC_DISABLED_HOOKS entry both skip it; defaults are preserved when unset.
_HP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -n "${_HP_DIR:-}" ] && [ -f "$_HP_DIR/hook-profile.sh" ]; then
    # shellcheck source=hooks/hook-profile.sh
    . "$_HP_DIR/hook-profile.sh"
    hook_should_run notify-telegram notification || exit 0
fi

# Resolve the parent repo name, stripping any `.claude/worktrees/<slug>` suffix
# so sub-agent notifications identify the real repo, not the worktree.
_repo_tag() {
    local TOPLEVEL
    TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null) || TOPLEVEL="$PWD"
    # Strip `/.claude/worktrees/<anything>` to reveal the parent repo path
    local PARENT="${TOPLEVEL%%/.claude/worktrees/*}"
    basename "$PARENT"
}

# Send a Telegram message. Credentials come from the environment first, then
# from ~/.claude/config/.env as a fallback (mirroring controller/notify.py).
_send_telegram() {
    local TITLE="${1:-Notification}"
    local BODY="${2:-}"
    local REPO
    REPO=$(_repo_tag)
    local TAGGED_TITLE="[${REPO}] ${TITLE}"

    # Environment wins; otherwise source ~/.claude/config/.env if present.
    # Silent no-op when neither yields credentials.
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        local ENV_FILE="${HOME}/.claude/config/.env"
        # shellcheck disable=SC1090
        [ -f "$ENV_FILE" ] && source "$ENV_FILE" 2>/dev/null || true
    fi

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        # Unconfigured: silent no-op.
        return 0
    fi

    if [ -n "${NOTIFY_DRYRUN:-}" ]; then
        # Test hook: report the resolved credentials without hitting the network.
        echo "[dry-run] telegram token=${TELEGRAM_BOT_TOKEN} chat=${TELEGRAM_CHAT_ID}"
        return 0
    fi

    # Build the payload with jq so quotes, backslashes, newlines, markdown
    # characters and emoji are escaped correctly. No string interpolation
    # of payload fields. parse_mode is omitted: plain text.
    local PAYLOAD
    PAYLOAD=$(jq -n \
        --arg chat "$TELEGRAM_CHAT_ID" \
        --arg text "${TAGGED_TITLE}
${BODY}" \
        '{chat_id: $chat, text: $text}')

    # Non-blocking error logging: a failed send appends one line to the log
    # instead of being swallowed, and never blocks the caller.
    if ! curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" > /dev/null 2>&1; then
        local LOG=~/.claude/logs/notify-telegram.log
        mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
        printf '%s telegram send failed: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$TAGGED_TITLE" \
            >> "$LOG" 2>/dev/null || true
    fi
}

_send_telegram "${1:-Notification}" "${2:-}"
exit 0
