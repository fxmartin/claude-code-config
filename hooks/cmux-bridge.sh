#!/bin/bash
# cmux-bridge.sh — Central utility for cmux sidebar integration
# Wraps cmux CLI with graceful degradation and optional Telegram fallback.
# Usage: cmux-bridge.sh <subcommand> [args...]

set -euo pipefail

# Graceful degradation: if cmux isn't available or socket is down, silently exit
if ! command -v cmux &>/dev/null || [ -z "${CMUX_SOCKET_PATH:-}" ]; then
    # Still handle telegram subcommand even without cmux
    if [ "${1:-}" = "telegram" ]; then
        shift
        TITLE="${1:-Notification}"
        BODY="${2:-}"
        source ~/.claude/config/.env 2>/dev/null || true
        if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
            curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                -H "Content-Type: application/json" \
                -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"${TITLE}\n${BODY}\", \"parse_mode\": \"Markdown\"}" > /dev/null 2>&1 || true
        fi
    fi
    exit 0
fi

SUBCOMMAND="${1:-}"
shift || true

case "$SUBCOMMAND" in
    status)
        # status <key> <text> [--icon name] [--color #hex]
        KEY="${1:-claude}"
        VALUE="${2:-}"
        shift 2 || true
        cmux set-status "$KEY" "$VALUE" "$@" 2>/dev/null || true
        ;;
    progress)
        # progress <value> [--label text]
        VALUE="${1:-0}"
        shift || true
        cmux set-progress "$VALUE" "$@" 2>/dev/null || true
        ;;
    log)
        # log <level> <message> [--source name]
        LEVEL="${1:-info}"
        MESSAGE="${2:-}"
        shift 2 || true
        cmux log --level "$LEVEL" "$@" -- "$MESSAGE" 2>/dev/null || true
        ;;
    notify)
        # notify <title> <body> — desktop only
        TITLE="${1:-Notification}"
        BODY="${2:-}"
        cmux notify --title "$TITLE" --body "$BODY" 2>/dev/null || true
        ;;
    telegram)
        # telegram <title> <body> — Telegram only, for long-running autonomous skills
        TITLE="${1:-Notification}"
        BODY="${2:-}"
        source ~/.claude/config/.env 2>/dev/null || true
        if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
            curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                -H "Content-Type: application/json" \
                -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"${TITLE}\n${BODY}\", \"parse_mode\": \"Markdown\"}" > /dev/null 2>&1 || true
        fi
        ;;
    clear)
        # clear [key] — clears status pill by key, or progress if no key
        KEY="${1:-}"
        if [ -n "$KEY" ]; then
            cmux clear-status "$KEY" 2>/dev/null || true
        else
            cmux clear-progress 2>/dev/null || true
        fi
        ;;
    *)
        # Unknown subcommand — silently ignore
        ;;
esac
