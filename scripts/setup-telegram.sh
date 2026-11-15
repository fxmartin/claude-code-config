#!/bin/bash
# ABOUTME: Writes ~/.claude/config/.env (mode 600) from 1Password for Telegram pings.
# ABOUTME: Prints only "written" on success; never echoes a credential.

set -euo pipefail

TOKEN_REF="${TELEGRAM_OP_TOKEN_REF:-op://Private/Telegram/bot token}"
CHAT_REF="${TELEGRAM_OP_CHAT_REF:-op://Private/Telegram/ID}"
ENV_FILE="${HOME}/.claude/config/.env"

command -v op >/dev/null 2>&1 || { echo "setup-telegram: 1Password CLI 'op' not found" >&2; exit 1; }

# stdin from /dev/null so a locked vault fails instead of prompting.
TOKEN=$(op read "$TOKEN_REF" </dev/null 2>/dev/null) || { echo "setup-telegram: could not read the bot token from 1Password" >&2; exit 1; }
CHAT=$(op read "$CHAT_REF" </dev/null 2>/dev/null) || { echo "setup-telegram: could not read the chat id from 1Password" >&2; exit 1; }
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "setup-telegram: 1Password returned an empty value" >&2; exit 1; }

mkdir -p "$(dirname "$ENV_FILE")"
umask 077
TMP=$(mktemp "${ENV_FILE}.XXXXXX")
trap 'rm -f "$TMP"' EXIT
printf 'TELEGRAM_BOT_TOKEN="%s"\nTELEGRAM_CHAT_ID="%s"\n' "$TOKEN" "$CHAT" > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$ENV_FILE"
trap - EXIT
echo "written"
