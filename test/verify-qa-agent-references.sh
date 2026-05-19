#!/usr/bin/env bash
# Verification check for story 1.1-001: Reconcile qa-expert -> qa-engineer.
#
# Asserts that no functional reference to the stale agent name `qa-expert`
# remains in skill files, agent definitions, command files, or CLAUDE.md.
# Story/epic spec documents under docs/stories/ are intentionally excluded:
# they describe the rename itself and editing them would corrupt the spec.
#
# Exit 0 = pass (no stale references), exit 1 = fail.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Functional surfaces that must never reference the stale agent name.
targets=(
  "CLAUDE.md"
  agents
  commands
  skills
  plugins
)

if matches=$(rg -F -n "qa-expert" "${targets[@]}" 2>/dev/null); then
  echo "FAIL: stale 'qa-expert' reference(s) found:" >&2
  echo "$matches" >&2
  exit 1
fi

echo "PASS: no stale 'qa-expert' references in functional surfaces."
