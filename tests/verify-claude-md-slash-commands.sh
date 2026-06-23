#!/usr/bin/env bash
# Verifies that every slash-command referenced in CLAUDE.md uses the bare-name
# form and resolves to an existing command or plugin skill file.
# Story 1.1-002: Reconcile slash-command naming in CLAUDE.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_MD="$REPO_ROOT/CLAUDE.md"
fail=0

assert_absent() {
  # $1 = stale namespaced command, $2 = reason
  if grep -Fq "$1" "$CLAUDE_MD"; then
    echo "FAIL: stale reference '$1' still present in CLAUDE.md ($2)"
    fail=1
  else
    echo "PASS: no stale reference '$1'"
  fi
}

assert_present() {
  # $1 = expected bare-name command
  if grep -Fq "$1" "$CLAUDE_MD"; then
    echo "PASS: expected reference '$1' present"
  else
    echo "FAIL: expected reference '$1' missing from CLAUDE.md"
    fail=1
  fi
}

resolves() {
  # $1 = bare command name (no leading slash)
  local name="$1"
  if [ -f "$REPO_ROOT/commands/$name.md" ]; then return 0; fi
  if find "$REPO_ROOT/commands" -type f -name "$name.md" | grep -q .; then return 0; fi
  if [ -f "$REPO_ROOT/plugins/autonomous-sdlc/skills/$name/SKILL.md" ]; then return 0; fi
  if [ -f "$REPO_ROOT/skills/$name/SKILL.md" ]; then return 0; fi
  # Shared skills (ADR-002) live in a single source of truth under
  # shared-skills/ and are exposed as bare top-level commands via committed
  # relative symlinks inside commands/ (so the commands/ check above usually
  # resolves them). This direct check covers the source of truth as well.
  if [ -f "$REPO_ROOT/shared-skills/$name.md" ]; then return 0; fi
  return 1
}

# Stale namespaced forms must be gone.
assert_absent "/issues:create-issue" "namespace removed; use /create-issue"
assert_absent "/quality:coverage" "namespace removed; use /coverage"
assert_absent "/project:create-project-summary-stats" "namespace removed; use /create-project-summary-stats"

# Corrected bare-name forms must be present.
assert_present "/create-issue"
assert_present "/coverage"
assert_present "/create-project-summary-stats"

# Every backtick-quoted slash-command referenced in CLAUDE.md must resolve to a
# file on disk. Only backtick-wrapped tokens are treated as command references;
# this avoids false positives from filesystem paths like `/dev/null`.
# shellcheck disable=SC2016  # single quotes are intentional: this is a regex
while IFS= read -r cmd; do
  name="${cmd#/}"
  if resolves "$name"; then
    echo "PASS: '$cmd' resolves to an existing command or skill file"
  else
    echo "FAIL: '$cmd' does not resolve to any command or plugin skill file"
    fail=1
  fi
done < <(grep -oE '`/[a-z][a-z0-9-]*`' "$CLAUDE_MD" | tr -d '`' | sort -u)

# Definition of Done: CHANGELOG.md must reference this story (#1.1-002) under
# the "Fixed" section, confirming the change was documented per the DoD.
CHANGELOG="$REPO_ROOT/CHANGELOG.md"
if [ ! -f "$CHANGELOG" ]; then
  echo "FAIL: CHANGELOG.md does not exist"
  fail=1
elif grep -qF "#1.1-002" "$CHANGELOG"; then
  echo "PASS: CHANGELOG.md documents story #1.1-002"
else
  echo "FAIL: CHANGELOG.md missing #1.1-002 entry — DoD requires changelog update"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "VERIFICATION FAILED"
  exit 1
fi
echo "VERIFICATION PASSED"
