#!/bin/bash
# Hook: SubagentStop — Clear pill, log completion, send desktop notification
# When a skill is active (sentinel file exists), skip the desktop notify to avoid spam.
INPUT=$(cat)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // "agent"')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // ""' | head -c 200)
SKILL_ACTIVE=$(cat /tmp/.claude-skill-active 2>/dev/null)

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" clear "agent-${AGENT_TYPE}"
if [ -z "$SKILL_ACTIVE" ]; then
    "$BRIDGE" notify "Agent Complete" "${AGENT_TYPE}: ${LAST_MSG}"
fi
