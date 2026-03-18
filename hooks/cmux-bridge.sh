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
    pane-create)
        # pane-create <label> [direction] — split a new pane, label it, print surface ref
        # Usage: cmux-bridge.sh pane-create "Story 01.2-001" [right|down]
        LABEL="${1:-agent}"
        DIRECTION="${2:-right}"
        OUTPUT=$(cmux new-split "$DIRECTION" 2>/dev/null) || exit 0
        # Parse: "OK surface:N workspace:N"
        SURFACE_REF=$(echo "$OUTPUT" | grep -oE 'surface:[0-9]+' | head -1)
        if [ -n "$SURFACE_REF" ]; then
            cmux rename-tab --surface "$SURFACE_REF" -- "$LABEL" >/dev/null 2>&1 || true
            echo "$SURFACE_REF"
        fi
        ;;
    pane-close)
        # pane-close <surface-ref> — close a specific surface
        # Usage: cmux-bridge.sh pane-close surface:4
        SURFACE_REF="${1:-}"
        if [ -n "$SURFACE_REF" ]; then
            cmux close-surface --surface "$SURFACE_REF" >/dev/null 2>&1 || true
        fi
        ;;
    pane-close-all)
        # pane-close-all <surface-refs...> — close multiple surfaces
        # Usage: cmux-bridge.sh pane-close-all surface:4 surface:5 surface:6
        shift 0 || true
        for REF in "$@"; do
            cmux close-surface --surface "$REF" >/dev/null 2>&1 || true
        done
        ;;
    *)
        # Unknown subcommand — silently ignore
        ;;
esac
