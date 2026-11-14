#!/bin/bash
# ABOUTME: Standalone Telegram notifier for autonomous skills (no terminal deps).
# ABOUTME: Usage: notify-telegram.sh <title> <body>. Logs once a day and no-ops when unconfigured.

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

# --- Credential resolution ----------------------------------------------------
# Order, per variable: environment, then ~/.claude/config/.env, then a
# non-interactive `op read` of 1Password. Mirrors controller/src/sdlc/notify.py.
# The .env file is parsed, never sourced, so it cannot run code.

TELEGRAM_OP_TOKEN_REF="${TELEGRAM_OP_TOKEN_REF:-op://Private/Telegram/bot token}"
TELEGRAM_OP_CHAT_REF="${TELEGRAM_OP_CHAT_REF:-op://Private/Telegram/ID}"
_OP_TIMEOUT="${TELEGRAM_OP_TIMEOUT:-5}"
_LOG_FILE="${HOME}/.claude/logs/notify-telegram.log"

# Print the value of NAME from the .env file ("" when absent or unreadable).
_file_value() {
    local NAME="$1" FILE="${HOME}/.claude/config/.env" LINE
    [ -f "$FILE" ] || return 0
    LINE=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${NAME}=" "$FILE" 2>/dev/null | tail -n 1) || return 0
    LINE="${LINE#*=}"
    LINE="${LINE%\"}"; LINE="${LINE#\"}"
    LINE="${LINE%\'}"; LINE="${LINE#\'}"
    printf '%s' "$LINE"
}

# Print the 1Password value for REF, or nothing. Never prompts or blocks:
# needs `op` and a timeout binary, stdin is /dev/null, output goes to a file
# (a pipe would stay open under a hung grandchild), and any failure is skipped.
_op_value() {
    local REF="$1" TMO OUT RC=0
    command -v op >/dev/null 2>&1 || return 0
    TMO=$(command -v timeout || command -v gtimeout) || return 0
    OUT=$(mktemp 2>/dev/null) || return 0
    "$TMO" "$_OP_TIMEOUT" op read "$REF" </dev/null >"$OUT" 2>/dev/null || RC=$?
    [ "$RC" -eq 0 ] && head -n 1 "$OUT"
    rm -f "$OUT"
    return 0
}

# Resolve credentials into TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID and record
# where each came from in TOKEN_SOURCE / CHAT_SOURCE (env|file|op|"").
_resolve_creds() {
    TOKEN_SOURCE="" CHAT_SOURCE=""
    local V
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then TOKEN_SOURCE="env"
    elif V=$(_file_value TELEGRAM_BOT_TOKEN) && [ -n "$V" ]; then TELEGRAM_BOT_TOKEN="$V"; TOKEN_SOURCE="file"
    elif V=$(_op_value "$TELEGRAM_OP_TOKEN_REF") && [ -n "$V" ]; then TELEGRAM_BOT_TOKEN="$V"; TOKEN_SOURCE="op"
    fi
    if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then CHAT_SOURCE="env"
    elif V=$(_file_value TELEGRAM_CHAT_ID) && [ -n "$V" ]; then TELEGRAM_CHAT_ID="$V"; CHAT_SOURCE="file"
    elif V=$(_op_value "$TELEGRAM_OP_CHAT_REF") && [ -n "$V" ]; then TELEGRAM_CHAT_ID="$V"; CHAT_SOURCE="op"
    fi
}

# Append one "unconfigured" line per calendar day so a silent host shows up.
_log_unconfigured() {
    local TODAY
    TODAY=$(date '+%Y-%m-%d')
    mkdir -p "$(dirname "$_LOG_FILE")" 2>/dev/null || return 0
    if [ -f "$_LOG_FILE" ] && grep -q "^${TODAY}T.* telegram unconfigured" "$_LOG_FILE" 2>/dev/null; then
        return 0
    fi
    printf '%s telegram unconfigured: no credentials from env, ~/.claude/config/.env or 1Password; run scripts/setup-telegram.sh\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$_LOG_FILE" 2>/dev/null || true
}

# Send a Telegram message. Never blocks or fails the caller.
_send_telegram() {
    local TITLE="${1:-Notification}"
    local BODY="${2:-}"
    local REPO
    REPO=$(_repo_tag)
    local TAGGED_TITLE="[${REPO}] ${TITLE}"

    local TOKEN_SOURCE CHAT_SOURCE
    _resolve_creds

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        _log_unconfigured
        return 0
    fi

    if [ -n "${NOTIFY_DRYRUN:-}" ]; then
        # Test hook: report where each credential resolved, never its value.
        echo "[dry-run] telegram token=resolved(${TOKEN_SOURCE}) chat=resolved(${CHAT_SOURCE})"
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
        local LOG="$_LOG_FILE"
        mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
        printf '%s telegram send failed: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$TAGGED_TITLE" \
            >> "$LOG" 2>/dev/null || true
    fi
}

_send_telegram "${1:-Notification}" "${2:-}"
exit 0
