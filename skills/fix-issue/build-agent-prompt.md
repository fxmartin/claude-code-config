# Build Agent Prompt

You are fixing GitHub issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

## Issue Details

{{ISSUE_BODY}}

## Investigation Results

- **Root Cause**: {{ROOT_CAUSE}}
- **Fix Approach**: {{FIX_APPROACH}}
- **Files to Modify**: {{FILES_TO_MODIFY}}
- **Complexity**: {{COMPLEXITY}}

## Instructions

### Step 1: Create Branch

```bash
git checkout -b fix/issue-{{ISSUE_NUMBER}}-$(echo "{{ISSUE_TITLE}}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | head -c 40)
```

### Step 2: Reproduce the Bug

Write a failing test that demonstrates the issue described in the bug report. This test MUST fail before your fix and pass after.

### Step 3: Implement the Fix

Follow the fix approach from the investigation. Keep changes minimal and focused:
- Fix only the root cause identified
- Do not refactor surrounding code
- Do not add unrelated improvements

### Step 4: Add Defensive Tests

Beyond the reproduction test, add tests for:
- Edge cases related to the fix
- Boundary values
- Error conditions the fix should handle
- Regression prevention

### Step 5: Run Quality Gates

Run all available quality checks:
```bash
# Tests
npm test || uv run pytest || make test
# Type checking (if applicable)
npx tsc --noEmit || uv run mypy . || true
# Linting (if applicable)
npx eslint . || uv run ruff check . || true
```

Fix any failures before proceeding.

### Step 6: Commit & Deliver

**If `{{SKIP_COVERAGE}}` is `true`** (build agent handles push + PR):

```bash
git add [specific files — not -A]
git commit -m "fix: [description]

Fixes #{{ISSUE_NUMBER}}

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"

git push -u origin [BRANCH_NAME]

gh pr create --title "fix: {{ISSUE_TITLE}} (#{{ISSUE_NUMBER}})" --body "$(cat <<'EOF'
## Summary
Fixes #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

**Root Cause**: {{ROOT_CAUSE}}
**Fix**: {{FIX_APPROACH}}

## Test plan
- [ ] Bug reproduction test confirms fix
- [ ] Edge cases and error paths tested
- [ ] All existing tests pass

Fixes #{{ISSUE_NUMBER}}

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Return `PR_NUMBER` and `PR_URL` when done.

**If `{{SKIP_COVERAGE}}` is `false`** (coverage agent handles push + PR):

```bash
git add [specific files — not -A]
git commit -m "fix: [description]

Fixes #{{ISSUE_NUMBER}}

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

DO NOT push or create a PR — the coverage agent handles that next.

Return `BRANCH_NAME` and `BUILD_STATUS: SUCCESS` when done.

## Output Contract

**If skip-coverage mode:**
```
PR_NUMBER: [number]
PR_URL: [url]
BUILD_STATUS: SUCCESS
```

**If coverage-enabled mode:**
```
BRANCH_NAME: fix/issue-{{ISSUE_NUMBER}}-[short-description]
BUILD_STATUS: SUCCESS
```
