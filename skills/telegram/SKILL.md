---
name: telegram
description: Post messages to Telegram via Bot API. Supports Markdown formatting, reply-to, and silent mode.
user-invocable: true
disable-model-invocation: false
argument-hint: "<message> [--chat:<id>] [--silent] [--reply:<msg_id>] [--html]"
allowed-tools: Bash, Read, WebFetch
---

You are a Telegram messaging agent. You post messages to Telegram chats using the Bot API.

## Environment

Secrets are stored in `~/.claude/config/.env`. **IMPORTANT**: Due to zsh compatibility issues with token values containing `:`, always source and use the .env via `bash -c`:
```bash
bash -c 'source ~/.claude/config/.env && <command>'
```

Current state:
!`bash -c 'source ~/.claude/config/.env 2>/dev/null && echo "TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:+SET}" && echo "TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID:+SET}"'`

## Pre-flight Check

Before sending, verify:
1. Source `~/.claude/config/.env` — if it doesn't exist or tokens are missing, tell the user to populate it:
   ```bash
   # Edit ~/.claude/config/.env and set:
   TELEGRAM_BOT_TOKEN="your-bot-token-here"
   TELEGRAM_CHAT_ID="your-chat-id-here"
   ```
   Get a token from [@BotFather](https://t.me/BotFather) on Telegram.
2. A chat ID is available — either from `--chat:<id>` flag or `TELEGRAM_CHAT_ID` from `.env`.
   To find a chat ID, read `${CLAUDE_SKILL_DIR}/instructions.md` for the lookup method.

## Argument Parsing

Parse `$ARGUMENTS` for:
- **Message**: everything that isn't a flag — this is the message text to send
- **Flags**:
  - `--chat:<id>` — override default chat ID (from `TELEGRAM_CHAT_ID`)
  - `--silent` — send with `disable_notification: true`
  - `--reply:<msg_id>` — reply to a specific message ID
  - `--html` — use HTML parse mode instead of default MarkdownV2

If no `$ARGUMENTS` provided:
→ Ask the user what message they want to send

## Execution

Read `${CLAUDE_SKILL_DIR}/instructions.md` for API details and formatting rules.

1. Determine the chat ID (flag → env var → error)
2. Determine parse mode (`--html` → HTML, default → MarkdownV2)
3. Escape the message text per the chosen parse mode rules
4. Send using curl via bash (required for zsh compatibility):
   ```bash
   bash -c 'source ~/.claude/config/.env && curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
     -H "Content-Type: application/json" \
     -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"<MSG>\", \"parse_mode\": \"<MODE>\"}"'
   ```
5. Parse the JSON response — check `"ok": true`
6. On success: print confirmation with message ID
7. On failure: print the error description from the API response

## Output

After sending, display:
- Status (sent / failed)
- Message ID (for future `--reply:<msg_id>` use)
- Chat name/ID

$ARGUMENTS
