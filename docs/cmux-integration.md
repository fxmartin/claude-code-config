# cmux Integration for Claude Code Multi-Agent Ecosystem

> Implemented 2026-03-18 | Targets cmux native macOS terminal

## Overview

This integration connects Claude Code's multi-agent orchestration system to cmux's sidebar UI, desktop notifications, and workspace management. It provides real-time visibility into long-running workflows (10-30 min) that previously had no feedback beyond terminal scrollback.

### What Changed

**Before**: Run `/fix-issue 42`, switch tabs, no feedback for 10-30 minutes. Permission prompts block silently. Telegram notification arrives at the end (if configured). Must scroll terminal to understand what happened.

**After**: Sidebar status pill shows current phase. Progress bar advances through each stage. Sidebar logs show key milestones. Permission prompts trigger desktop notifications. Agent completion triggers desktop + Telegram.

## Architecture

### Central Utility: `hooks/cmux-bridge.sh`

Single entry point for all cmux sidebar interaction. Every subcommand follows the same pattern:

1. **Graceful degradation**: If `cmux` binary isn't found or `CMUX_SOCKET_PATH` is unset, silently exit (no errors, no blocking)
2. **Telegram fallback**: The `notify` subcommand always attempts Telegram delivery even without cmux
3. **Silent failures**: All cmux CLI calls are wrapped in `|| true` to never break caller workflows

#### Subcommands

| Subcommand | Usage | What it does |
|-----------|-------|-------------|
| `status` | `cmux-bridge.sh status <key> <text> [--icon name] [--color #hex]` | Set a sidebar status pill |
| `progress` | `cmux-bridge.sh progress <0.0-1.0> [--label text]` | Set sidebar progress bar |
| `log` | `cmux-bridge.sh log <level> <message> [--source name]` | Append sidebar log entry |
| `notify` | `cmux-bridge.sh notify <title> <body>` | Desktop notification + Telegram |
| `clear` | `cmux-bridge.sh clear [key]` | Clear status pill (by key) or progress bar (no key) |

### Hook Scripts

Automatic lifecycle tracking that fires for ALL skills and agents without per-skill modification. Configured in `settings.json` under the `hooks` key.

| Script | Hook Event | Behavior |
|--------|-----------|----------|
| `cmux-session-start.sh` | `SessionStart` | Logs "Claude session started", sets "Ready" status pill (blue) |
| `cmux-agent-start.sh` | `SubagentStart` | Shows "Running: {agent_type}" status pill (blue), logs agent start |
| `cmux-agent-stop.sh` | `SubagentStop` | Clears agent pill, logs completion, sends desktop notification with result excerpt (first 200 chars) |
| `cmux-stop.sh` | `Stop` | Clears all progress/status, sends "Claude Done" notification |
| `cmux-permission.sh` | `Notification` (matcher: `permission_prompt`) | Sets red "Permission Needed" pill, sends urgent desktop notification |

All hooks receive JSON on stdin from Claude Code with context like `agent_type`, `last_assistant_message`, and `notification_type`. All run asynchronously (never block Claude).

### Hook Configuration in `settings.json`

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/cmux-session-start.sh" }] }],
    "SubagentStart": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/cmux-agent-start.sh" }] }],
    "SubagentStop": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/cmux-agent-stop.sh" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/cmux-stop.sh" }] }],
    "Notification": [{ "matcher": "permission_prompt", "hooks": [{ "type": "command", "command": "~/.claude/hooks/cmux-permission.sh" }] }]
  }
}
```

## Skill Integrations

### `/fix-issue` — 11-Phase Progress Tracking

Each phase updates the sidebar progress bar and status pill:

| Phase | Progress | Status Text | Color |
|-------|----------|------------|-------|
| 1. Validate | 0.09 | "Validating issue" | Blue `#007AFF` |
| 2. Fetch issue | 0.18 | "Fetching issue" | Blue |
| 3. Investigation | 0.27 | "Investigating" | Blue |
| 4. Build | 0.36 | "Building fix" | Orange `#FF9500` |
| 5. Coverage | 0.45 | "Coverage check" | Orange |
| 6. Review | 0.64 | "Code review" | Orange |
| 7. E2E | 0.73 | "E2E testing" | Orange |
| 8. Bugfix loop | — | "Bugfix loop" | Red `#FF3B30` |
| 9. Merge | 0.82 | "Merging" | Green `#34C759` |
| 10. Summary | 0.91 | "Summarizing" | Green |
| 11. Complete | 1.0 | "Complete" | Green |

Start and completion notifications are sent via `cmux-bridge.sh notify` (desktop + Telegram).

### `/build-stories` — Per-Story Progress

- **Phase 1**: Status pill "Starting", progress bar at 0.0
- **Phase 2**: Status pill "Discovering stories"
- **Phase 5 (build loop)**: Status pill "Building stories"
  - Per-story progress: `current/total` as decimal fraction
  - Per-story status pills: `story-{ID}` with green (success) or red (failure)
  - Per-story sidebar logs with success/error level
  - Per-story desktop + Telegram notifications
- **Phase 7**: Progress 1.0, status "Complete"

## Notification Flow

All notifications now route through `cmux-bridge.sh notify`, which handles dual-channel delivery:

```
cmux-bridge.sh notify "Title" "Body"
  |
  +-> cmux notify --title "Title" --body "Body"     (desktop notification)
  |
  +-> curl Telegram API                               (if TELEGRAM_BOT_TOKEN set)
```

No direct Telegram `curl` blocks remain in any skill except the dedicated `/telegram` skill itself.

## Files

### Created

| File | Lines | Purpose |
|------|-------|---------|
| `hooks/cmux-bridge.sh` | ~78 | Central utility with 5 subcommands |
| `hooks/cmux-session-start.sh` | ~6 | SessionStart lifecycle hook |
| `hooks/cmux-agent-start.sh` | ~8 | SubagentStart lifecycle hook |
| `hooks/cmux-agent-stop.sh` | ~10 | SubagentStop lifecycle hook |
| `hooks/cmux-stop.sh` | ~8 | Stop lifecycle hook |
| `hooks/cmux-permission.sh` | ~6 | Permission prompt notification hook |

### Modified

| File | Changes |
|------|---------|
| `settings.json` | Added `hooks` configuration block (5 event types) |
| `skills/fix-issue/SKILL.md` | Added bridge calls at 11 phase boundaries, replaced 2 Telegram curl blocks |
| `skills/build-stories/SKILL.md` | Added bridge calls at phase boundaries + per-story progress, replaced 3 Telegram curl blocks |

## Verification

All subcommands were tested live against the cmux socket:

```bash
# Status pill
cmux-bridge.sh status test-pill "Integration test" --icon sparkle --color "#007AFF"  # OK

# Progress bar
cmux-bridge.sh progress 0.42 --label "Testing bridge"  # OK

# Sidebar log
cmux-bridge.sh log success "Bridge test passed" --source test  # OK

# Desktop notification
cmux-bridge.sh notify "cmux Bridge Test" "All subcommands working"  # OK

# Graceful degradation (no socket)
CMUX_SOCKET_PATH="" cmux-bridge.sh status test "hello"  # Exit 0, no error

# JSON validation
cat settings.json | python3 -m json.tool  # Valid JSON
```
