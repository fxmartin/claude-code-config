# Telegram Bot API Instructions

## Sending Messages

Endpoint: `POST https://api.telegram.org/bot<token>/sendMessage`

Required parameters:
- `chat_id` — integer or string (e.g., `-1001234567890` for groups, `@channelname` for public channels)
- `text` — message text (up to 4096 characters)

Optional parameters:
- `parse_mode` — `MarkdownV2` or `HTML`
- `disable_notification` — `true` for silent delivery
- `reply_to_message_id` — integer, reply to a specific message

## MarkdownV2 Escaping Rules

In MarkdownV2, these characters MUST be escaped with `\` when used as literal text
(not as formatting markers):

```
_ * [ ] ( ) ~ ` > # + - = | { } . !
```

Formatting markers:
- `*bold*`
- `_italic_`
- `__underline__`
- `~strikethrough~`
- `||spoiler||`
- `` `inline code` ``
- ` ```pre``` ` (code block)
- `[text](url)` (link)

**Important**: When escaping for JSON + MarkdownV2, backslashes need double-escaping
in the JSON string (e.g., `\\*` in JSON becomes `\*` in the API).

## HTML Formatting

Supported tags:
- `<b>bold</b>`, `<strong>bold</strong>`
- `<i>italic</i>`, `<em>italic</em>`
- `<u>underline</u>`
- `<s>strikethrough</s>`
- `<code>inline code</code>`
- `<pre>code block</pre>`
- `<a href="url">text</a>`

HTML special characters (`<`, `>`, `&`) must be escaped as `&lt;`, `&gt;`, `&amp;`.

## Finding Your Chat ID

### For private chats / groups:
1. Send any message to your bot (or add it to the group)
2. Run:
   ```bash
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | jq '.result[-1].message.chat.id'
   ```

### For channels:
- Use `@channelname` as the chat ID (bot must be admin)
- Or forward a channel message to @userinfobot

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `chat not found` | Invalid chat ID or bot not added | Add bot to group/channel |
| `bot was blocked by the user` | User blocked the bot | User must unblock |
| `can't parse entities` | Bad MarkdownV2 escaping | Check escape rules above |
| `Forbidden: bot is not a member` | Bot not in group/channel | Add bot as member/admin |

## Response Format

Success:
```json
{
  "ok": true,
  "result": {
    "message_id": 123,
    "chat": {"id": -1001234567890, "title": "My Group"},
    "text": "Hello!"
  }
}
```

Failure:
```json
{
  "ok": false,
  "error_code": 400,
  "description": "Bad Request: can't parse entities..."
}
```
