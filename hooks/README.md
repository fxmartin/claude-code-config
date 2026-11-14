# Claude Code Hooks

Place hook configuration files here. Hooks are shell commands that execute in response to Claude Code events.

## Structure

Hook configs should be JSON files defining triggers and commands.

## Session pings

`notify-session-event.sh` is wired to the `Notification` and `Stop` events in
`settings.template.json` (`install.sh --core` merges it into an existing
`settings.json`). It reads the hook JSON on stdin and hands a title and body to
`notify-telegram.sh`, so a session waiting for input, or one that finished a long
turn, pings Telegram.

- `Notification` pings "Waiting for you" with host, tmux session and the message
  (200 characters, tokens masked).
- `Stop` pings "Finished" only when the turn ran at least
  `CLAUDE_NOTIFY_MIN_TURN_SECONDS` (default 120) since the last user prompt in
  the transcript; an unknown duration sends nothing.
- At most one ping per session and event every 60 seconds (state under
  `~/.claude/state/`).
- Silent when `SDLC_BATCH_BUILD` is set, `stop_hook_active` is true, or
  `CLAUDE_NOTIFY_SESSION=0`.
- Always exits 0 with no stdout. Needs `bash` and `jq`.

Tests: `tests/notify-session-event.bats`.

## Tuning strictness

Hook strictness, a per-hook disable list, and the SessionStart context size are
all tunable from the environment via `hook-profile.sh` — no script edits needed.
See [Hook profiles & context controls](../docs/hook-profiles.md).

## Documentation

See [Claude Code hooks documentation](https://docs.anthropic.com/en/docs/claude-code/hooks) for details.
