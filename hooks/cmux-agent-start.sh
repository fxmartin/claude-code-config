#!/bin/bash
# Hook: SubagentStart — Show status pill for running agent
INPUT=$(cat)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // "agent"')

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" status "agent-${AGENT_TYPE}" "Running: ${AGENT_TYPE}" --icon hammer --color "#007AFF"
"$BRIDGE" log progress "Agent started: ${AGENT_TYPE}" --source claude
