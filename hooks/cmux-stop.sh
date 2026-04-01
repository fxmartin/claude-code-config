#!/bin/bash
# Hook: Stop — Fires after each Claude response completes.
# Clear progress bar and any stale permission pill.
# When a skill is active (sentinel file exists), skip progress clear — skill manages it.
cat > /dev/null
SKILL_ACTIVE=$(cat /tmp/.claude-skill-active 2>/dev/null)

if [ -z "$SKILL_ACTIVE" ]; then
    ~/.claude/hooks/cmux-bridge.sh clear
fi
~/.claude/hooks/cmux-bridge.sh clear claude
