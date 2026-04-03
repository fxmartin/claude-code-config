#!/bin/bash
# PostToolUse hook: trigger documentation update after a standalone PR merge
# Gated by the build-stories sentinel — skipped during batch builds.
set -e

INPUT=$(cat)

# Extract fields from the hook payload
EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exitCode // "1"')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Only proceed if the command succeeded
if [ "$EXIT_CODE" -ne 0 ]; then
  exit 0
fi

# Guard: skip if build-stories skill is running (it handles docs in its own phase)
if [ -f /tmp/.claude-skill-active ]; then
  exit 0
fi

# Extract PR number from command (gh pr merge 42, gh pr merge #42, etc.)
PR_NUM=$(echo "$COMMAND" | grep -oP '(?:gh pr merge\s+#?)(\d+)' | grep -oP '\d+' || echo "unknown")

# Extract merge output for context
STDOUT=$(echo "$INPUT" | jq -r '.tool_response.stdout // ""' | head -5)

# Inject additionalContext so Claude updates documentation
jq -n \
  --arg pr_num "$PR_NUM" \
  --arg stdout "$STDOUT" \
  '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "PR #\($pr_num) was just merged successfully.\n\nPlease update documentation:\n1. Check if README.md needs updating (new features, changed APIs, updated setup steps)\n2. Check if STORIES.md or epic files need status updates\n3. If changes are needed, make the edits, commit with message: docs: update documentation for PR #\($pr_num)\n4. If no documentation changes are needed, say so briefly and move on.\n\nMerge output: \($stdout)"
    }
  }'

exit 0
