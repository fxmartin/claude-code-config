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

PASS=0
FAIL=0

pass() { echo "PASS: $1"; ((PASS++)) || true; }
fail() { echo "FAIL: $1" >&2; ((FAIL++)) || true; }

# Functional surfaces that must never reference the stale agent name.
targets=(
  "CLAUDE.md"
  agents
  commands
  skills
  plugins
)

# AC3: rg returns zero matches for qa-expert in functional surfaces.
if matches=$(rg -F -n "qa-expert" "${targets[@]}" 2>/dev/null); then
  fail "stale 'qa-expert' reference(s) found:
$matches"
else
  pass "no stale 'qa-expert' references in functional surfaces"
fi

# AC2a: agents/qa-expert.md must NOT exist (no stale agent file created).
if [[ -f "agents/qa-expert.md" ]]; then
  fail "agents/qa-expert.md must not exist — stale agent file found"
else
  pass "agents/qa-expert.md does not exist"
fi

# AC2b: agents/qa-engineer.md frontmatter name must be 'qa-engineer'.
canonical_name=$(grep -m1 '^name:' agents/qa-engineer.md | sed 's/name:[[:space:]]*//')
if [[ "$canonical_name" == "qa-engineer" ]]; then
  pass "agents/qa-engineer.md frontmatter name is canonical ('qa-engineer')"
else
  fail "agents/qa-engineer.md frontmatter name is '$canonical_name', expected 'qa-engineer'"
fi

# DoD: CHANGELOG.md must document the fix under 'Fixed'.
if rg -q "qa-expert" CHANGELOG.md 2>/dev/null && rg -q "Fixed" CHANGELOG.md 2>/dev/null; then
  pass "CHANGELOG.md documents the qa-expert → qa-engineer fix under Fixed"
else
  fail "CHANGELOG.md missing qa-expert → qa-engineer entry under Fixed"
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed."
[[ $FAIL -eq 0 ]]
