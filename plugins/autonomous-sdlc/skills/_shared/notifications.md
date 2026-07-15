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
  Telegram-only and a **silent no-op when unconfigured**, so no guard or
  configuration check is needed.
- Notification failures are best-effort: they must never block or fail the
  skill's own work.
- There are no sidebar or desktop notifications.
