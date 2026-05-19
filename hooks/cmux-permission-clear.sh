#!/bin/bash
# Hook: PostToolUse — Clear "Permission Needed" pill after tool executes (meaning permission was granted)
cat > /dev/null

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" clear claude
