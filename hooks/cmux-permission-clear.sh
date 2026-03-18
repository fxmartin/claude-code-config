#!/bin/bash
# Hook: PostToolUse — Clear "Permission Needed" pill after tool executes (meaning permission was granted)
INPUT=$(cat)

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" clear claude
