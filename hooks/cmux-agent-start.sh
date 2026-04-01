#!/bin/bash
# Hook: SubagentStart — Show status pill for running agent
# When a skill is active (sentinel file exists), skip the pill to avoid accumulation.
INPUT=$(cat)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // "agent"')
SKILL_ACTIVE=$(cat /tmp/.claude-skill-active 2>/dev/null)

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
if [ -z "$SKILL_ACTIVE" ]; then
    "$BRIDGE" status "agent-${AGENT_TYPE}" "Running: ${AGENT_TYPE}" --icon hammer --color "#007AFF"
fi
"$BRIDGE" log progress "Agent started: ${AGENT_TYPE}" --source claude
