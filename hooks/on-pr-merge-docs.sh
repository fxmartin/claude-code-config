#!/bin/bash
# PostToolUse hook: trigger documentation update after a standalone PR merge
# Gated by SDLC_BATCH_BUILD — skipped during controller batch builds.
set -e

INPUT=$(cat)

# Extract fields from the hook payload
EXIT_CODE=$(jq -r '.tool_response.exitCode // "1"' <<<"${INPUT}")
COMMAND=$(jq -r '.tool_input.command // ""' <<<"${INPUT}")

# Only proceed if the command succeeded
if [[ "${EXIT_CODE}" -ne 0 ]]; then
  exit 0
fi

# Guard: skip during a controller batch build (issue #214). Dispatched agents are
# marked with SDLC_BATCH_BUILD; the build handles docs in its own phase, so this
# hook must not inject doc-update context (which makes the merge agent commit the
# regenerated build-progress render onto the checked-out branch).
if [[ -n "${SDLC_BATCH_BUILD:-}" ]]; then
  exit 0
fi

# Extract PR number from command (gh pr merge 42, gh pr merge #42, etc.)
PR_NUM="unknown"
if [[ "${COMMAND}" =~ gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+\#?([0-9]+) ]]; then
  PR_NUM="${BASH_REMATCH[1]}"
fi

# Extract merge output for context
STDOUT_RAW=$(jq -r '.tool_response.stdout // ""' <<<"${INPUT}")
STDOUT=""
line_count=0
while IFS= read -r line && [[ "${line_count}" -lt 5 ]]; do
  if [[ "${line_count}" -gt 0 ]]; then
    STDOUT+=$'\n'
  fi
  STDOUT+="${line}"
  line_count=$((line_count + 1))
done <<<"${STDOUT_RAW}"

# Inject additionalContext so Claude updates documentation
jq -n \
  --arg pr_num "${PR_NUM}" \
  --arg stdout "${STDOUT}" \
  '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "PR #\($pr_num) was just merged successfully.\n\nPlease update documentation:\n1. Check if README.md needs updating (new features, changed APIs, updated setup steps)\n2. Check if STORIES.md or epic files need status updates\n3. If changes are needed, make the edits, commit with message: docs: update documentation for PR #\($pr_num)\n4. If no documentation changes are needed, say so briefly and move on.\n\nMerge output: \($stdout)"
    }
  }'

exit 0
