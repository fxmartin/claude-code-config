# Claude Code Hooks

Place hook configuration files here. Hooks are shell commands that execute in response to Claude Code events.

## Structure

Hook configs should be JSON files defining triggers and commands.

## Tuning strictness

Hook strictness, a per-hook disable list, and the SessionStart context size are
all tunable from the environment via `hook-profile.sh` — no script edits needed.
See [Hook profiles & context controls](../docs/hook-profiles.md).

## Documentation

See [Claude Code hooks documentation](https://docs.anthropic.com/en/docs/claude-code/hooks) for details.
