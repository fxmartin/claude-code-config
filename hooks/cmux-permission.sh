#!/bin/bash
# Hook: Notification (permission_prompt) — Alert dev that permission is needed
INPUT=$(cat)

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" status claude "Permission Needed" --icon sparkle --color "#FF3B30"
"$BRIDGE" notify "Permission Needed" "Claude Code is waiting for your approval"
