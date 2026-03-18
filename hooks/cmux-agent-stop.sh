#!/bin/bash
# Hook: SubagentStop — Clear pill, log completion, send desktop notification
INPUT=$(cat)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // "agent"')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // ""' | head -c 200)

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"
"$BRIDGE" clear "agent-${AGENT_TYPE}"
"$BRIDGE" log success "Agent done: ${AGENT_TYPE}" --source claude
"$BRIDGE" notify "Agent Complete" "${AGENT_TYPE}: ${LAST_MSG}"
