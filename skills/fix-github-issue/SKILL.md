---
name: fix-github-issue
description: Fix a GitHub issue with quality gates — coverage, code review, E2E, and bugfix triage loop.
user-invocable: true
disable-model-invocation: true
argument-hint: "<issue-number|issue-url|next> [--skip-coverage] [--e2e-gate=block|warn|off] [--auto]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context.

## Phase 1: Parse Arguments & Validate Environment (DIRECT)

Parse `$ARGUMENTS` for:
- **Target**: issue number (e.g., `123`), issue URL (e.g., `https://github.com/owner/repo/issues/123`), or `next` (highest priority open bug)
- **Flags**:
  - `--skip-coverage` — bypass coverage gate (build agent creates PR directly)
  - `--e2e-gate=block|warn|off` — E2E test gate behavior (default: `off`)
  - `--auto` — skip interactive prompts on failure

Extract the issue number from the argument:
- If URL: extract number from path
- If `next`: run `gh issue list --label bug --state open --limit 1 --json number,title -q '.[0]'` to get the highest priority open bug
- If number: use directly

Run these validation checks directly:

```bash
# 1. GitHub CLI auth
gh auth status
# 2. Clean git state
git status --porcelain
# 3. On main branch
git branch --show-current
# 4. Pull latest
git pull
```

STOP if working tree is dirty or not on main branch.

## Phase 2: Fetch Issue & Validate (DIRECT)

```bash
gh issue view $ISSUE_NUMBER --json number,title,body,state,assignees,labels
```

**Stop conditions** — STOP and report if:
- Issue is `closed`
- Issue is assigned to someone else (not the current `gh` user)
- Issue has label `wontfix` or `won't fix`

Extract: `ISSUE_NUMBER`, `ISSUE_TITLE`, `ISSUE_BODY`, `ISSUE_LABELS`.

## Phase 3: Investigation (DIRECT)

Investigate the issue yourself (this is lightweight context gathering):

1. Extract key details: reproduction steps, error messages, stack traces, affected files
2. Search for relevant files using keywords from the issue
3. Check for linked PRs or related issues: `gh issue list --search "mentions:#$ISSUE_NUMBER"`
4. Review recent commits touching affected files
5. Assess complexity (simple / moderate / complex)

## Phase 4: Fix Plan & Approval Gate (DIRECT)

Present a structured plan:

```
### Proposed Fix for #[ISSUE_NUMBER]: [ISSUE_TITLE]

**Root Cause**: [your diagnosis]
**Approach**: [strategy in 1-2 sentences]
**Complexity**: Simple / Moderate / Complex

**Files to Modify**:
- `path/to/file` — [what changes]

**New Files** (if any):
- `path/to/new` — [purpose]

**Tests**:
- [ ] Update existing: [which]
- [ ] Add new: [describe coverage]

**Risk Assessment**: Low / Medium / High + rationale

**Quality Gates**:
- Coverage gate: [enabled / skipped]
- E2E gate: [off / warn / block]
```

**STOP and wait for user approval before proceeding.**

## Phase 5: Build Agent

Launch the appropriate agent (detect from project type — `backend-typescript-architect` for TS/Bun, `python-backend-engineer` for Python, `general-purpose` as fallback).

**If `--skip-coverage` is set** (build agent handles push + PR):

```
You are fixing GitHub issue #[ISSUE_NUMBER]: [ISSUE_TITLE]

## Issue Details
[ISSUE_BODY]

## Fix Plan
[The approved plan from Phase 4]

## Instructions
1. Create branch: git checkout -b fix/issue-[ISSUE_NUMBER]-[short-description]
2. Reproduce the bug first — create a failing test that demonstrates the issue
3. Implement the minimal fix
4. Add defensive tests — edge cases, regression prevention
5. Run all quality gates (tests, types, lint)
6. Commit:
   git add [specific files]
   git commit -m "fix: [description]

   Fixes #[ISSUE_NUMBER]

   Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
7. Push and create PR:
   git push -u origin fix/issue-[ISSUE_NUMBER]-[short-description]
   gh pr create --title "fix: [ISSUE_TITLE] (#[ISSUE_NUMBER])" --body "[summary, testing, Fixes #[ISSUE_NUMBER]]"

Return PR_NUMBER: [number] and PR_URL: [url] when done.
```

Extract `PR_NUMBER` from the agent result. Skip Phase 6.

**If coverage gate is enabled** (default — build agent commits locally only):

```
You are fixing GitHub issue #[ISSUE_NUMBER]: [ISSUE_TITLE]

## Issue Details
[ISSUE_BODY]

## Fix Plan
[The approved plan from Phase 4]

## Instructions
1. Create branch: git checkout -b fix/issue-[ISSUE_NUMBER]-[short-description]
2. Reproduce the bug first — create a failing test that demonstrates the issue
3. Implement the minimal fix
4. Add defensive tests — edge cases, regression prevention
5. Run all quality gates (tests, types, lint)
6. Commit locally:
   git add [specific files]
   git commit -m "fix: [description]

   Fixes #[ISSUE_NUMBER]

   Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
7. DO NOT push or create a PR — the coverage agent handles that next.

Return BRANCH_NAME: fix/issue-[ISSUE_NUMBER]-[short-description] and BUILD_STATUS: SUCCESS when done.
```

Extract `BRANCH_NAME` and `BUILD_STATUS` from the agent result. Proceed to Phase 6.

## Phase 6: Coverage Gate (skip if `--skip-coverage`)

Launch a `qa-expert` agent with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` -> current issue number
- `{{ISSUE_TITLE}}` -> current issue title
- `{{BRANCH_NAME}}` -> branch name from build agent result

The agent runs coverage analysis, adds tests, pushes the branch, and creates the PR.

Extract `PR_NUMBER`, `PR_URL`, `COVERAGE_PCT`, `TESTS_ADDED`, and `COVERAGE_STATUS` from the agent result.

If `COVERAGE_STATUS: WARN` — log a warning but continue.
If the coverage agent fails entirely — proceed to Phase 9 (bugfix loop).

## Phase 7: Review Gate

Launch a `senior-code-reviewer` agent with the prompt from `${CLAUDE_SKILL_DIR}/review-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` -> current issue number
- `{{ISSUE_TITLE}}` -> current issue title
- `{{PR_NUMBER}}` -> PR number from Phase 5 (if `--skip-coverage`) or Phase 6 (default)

The agent reviews the PR, fixes issues if found, and approves when satisfied.

Extract `APPROVAL_STATUS` from the agent result.

If `APPROVAL_STATUS: CHANGES_NEEDED` persists after the review agent's fixes, proceed to Phase 9 (bugfix loop).

## Phase 8: E2E Gate (skip unless `--e2e-gate=block|warn`)

Launch a `qa-expert` agent with the prompt from `${CLAUDE_SKILL_DIR}/e2e-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` -> current issue number
- `{{ISSUE_TITLE}}` -> current issue title
- `{{PR_NUMBER}}` -> PR number
- `{{BRANCH_NAME}}` -> branch name

Handle result per `--e2e-gate` mode:
- `block` + FAIL: if `--auto` treat as `warn`, otherwise ask user: retry / continue / abort
- `warn` + FAIL: log warning, continue
- PASS: continue

If FAIL and not continuing, proceed to Phase 9 (bugfix loop).

## Phase 9: Bugfix Loop (on any gate failure)

Launch a `general-purpose` agent with the prompt from `${CLAUDE_SKILL_DIR}/bugfix-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` -> current issue number
- `{{ISSUE_TITLE}}` -> current issue title
- `{{BRANCH_NAME}}` -> the fix branch
- `{{FAILED_STEP}}` -> which phase failed (build | coverage | review | e2e)
- `{{FAILURE_OUTPUT}}` -> the error/failure text from the failed agent

Extract `FAILURE_CATEGORY`, `ISSUE_NUMBER` (sub-issue), `FIX_STATUS`, `TESTS_PASSING` from the agent result.

- If `FIX_STATUS: FIXED` and `TESTS_PASSING: true`:
  - Log: "Fixed ([FAILURE_CATEGORY]) — retrying from failed step"
  - **Re-run from the phase that failed** (not from Phase 5):
    - If build failed -> re-run Phase 5
    - If coverage failed -> re-run Phase 6
    - If review flagged issues -> re-run Phase 7
    - If E2E failed -> re-run Phase 8
  - Allow **max 2 bugfix iterations** to prevent infinite loops
- If `FIX_STATUS: UNFIXED`:
  - If `--auto`: log failure and STOP
  - Otherwise: ask user what to do

## Phase 10: Merge & Cleanup (DIRECT)

Once all gates pass:

```bash
# Merge PR
gh pr merge $PR_NUMBER --squash --delete-branch

# Comment on and close original issue
gh issue comment $ISSUE_NUMBER --body "Fixed in PR #$PR_NUMBER."
gh issue close $ISSUE_NUMBER --reason completed

# Return to main
git checkout main
git pull
```

## Phase 11: Summary Output (DIRECT)

Print a structured summary:

```
## Fix Complete: #[ISSUE_NUMBER] — [ISSUE_TITLE]

- **PR**: #[PR_NUMBER] ([PR_URL])
- **Branch**: [BRANCH_NAME] (merged & deleted)
- **Files Modified**: [count]
- **Tests Added**: [count]
- **Coverage**: [COVERAGE_PCT]% [COVERAGE_STATUS]
- **Review**: [APPROVAL_STATUS]
- **E2E**: [E2E_RESULT or "skipped"]
- **Bugfix Iterations**: [count]
```

## Context Budget Rules

**NEVER read project source files directly** — the build agents read them.
Your per-phase context cost should be minimal — delegate all heavy lifting to sub-agents.

## Context

$ARGUMENTS
