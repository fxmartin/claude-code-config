# Shared Notification Contract

<!-- ABOUTME: Single source of truth for the Telegram-notification boilerplate
     that was previously duplicated verbatim at the top of every notifying
     skill (Story 27.1-004). Skills reference this file instead of restating it. -->

Skills that reference this contract send Telegram pings at lifecycle
milestones via:

```bash
bash -c '~/.claude/hooks/notify-telegram.sh "<title>" "<body>"'
```

Rules:

- Call the hook **unconditionally** at each milestone the skill marks — it is
  Telegram-only and a **silent no-op when unconfigured** (on the skill side; the host logs it), so no guard or
  configuration check is needed. An unconfigured host is never silent: the hook
  appends one line a day to `~/.claude/logs/notify-telegram.log` and
  `sdlc doctor` reports `telegram: unconfigured` with the fix.
- Notification failures are best-effort: they must never block or fail the
  skill's own work.
- There are no sidebar or desktop notifications.

## Credentials

Resolved per variable, in this order (the hook and `controller/src/sdlc/notify.py` agree):

1. environment: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
2. `~/.claude/config/.env`
3. 1Password via `op read` of `op://Private/Telegram/bot token` and
   `op://Private/Telegram/ID` (override with `TELEGRAM_OP_TOKEN_REF` /
   `TELEGRAM_OP_CHAT_REF`). It is used only when `op` is on PATH and answers
   non-interactively (stdin `/dev/null`, 5 s timeout, `TELEGRAM_OP_TIMEOUT` to
   change it); otherwise it is skipped, never prompted.

One setup command writes `~/.claude/config/.env` (mode 600) from 1Password and
prints only `written`:

```bash
scripts/setup-telegram.sh
```

`NOTIFY_DRYRUN=1` reports which source resolved each credential
(`env`, `file`, `op`), never the values.
