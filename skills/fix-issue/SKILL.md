---
name: fix-issue
description: Fully autonomous issue fixer — thin orchestrator that delegates investigation, build, quality gates, merge, and summary to sub-agents.
user-invocable: true
disable-model-invocation: false
argument-hint: "<issue-number|issue-url|next|all> [--skip-coverage] [--e2e-gate=warn|off] [--skip-e2e] [--limit=N] [--coverage-threshold=N] [--skip-preflight]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context.

## Phase 1: Parse Arguments & Validate Environment (DIRECT)

Parse `$ARGUMENTS` for:
- **Target**: issue number (e.g., `123`), issue URL (e.g., `https://github.com/owner/repo/issues/123`), `next` (highest priority open bug), or `all`/`opened issues` (all open issues sequentially)
- **Flags**:
  - `--skip-coverage` — bypass coverage gate (build agent creates PR directly)
  - `--e2e-gate=warn|off` — E2E test gate behavior (default: `off`)
  - `--skip-e2e` — shorthand for `--e2e-gate=off`
  - `--limit=N` — when using `next`, process up to N open bugs sequentially
  - `--coverage-threshold=N` — override the default 90% coverage threshold
  - `--skip-preflight` — skip the pre-flight health check

**Removed flags** (fully autonomous mode — no user interaction):
- `--confirm` is removed — always proceed autonomously after investigation
- `--auto` is removed — autonomous behavior is now the default

Extract the issue number from the argument:
- If URL: extract number from path
- If `next`: run `gh issue list --label bug --state open --limit 1 --json number,title -q '.[0]'` to get the highest priority open bug
- If `all` or `opened issues`: run `gh issue list --state open --json number,title,labels --limit 50` to get all open issues, then process each sequentially (bugs first, then enhancements by priority)
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

### Pre-Flight Health Check (skip if `--skip-preflight`)

Before dispatching any agents, verify the project's test suite is green on main:

1. **Detect test command**: Check for test scripts in order of preference:
   - `package.json` → `scripts.test` → run `npm test`
   - `pyproject.toml` → detect pytest → run `uv run pytest`
   - `Makefile` → check for `test` target → run `make test`
   - `bats` test files → run `bats test/`
2. **Run tests**: Execute the detected test command
3. **Fail-fast if red**: If tests fail on main before any work starts, STOP with:
   ```
   PRE_FLIGHT_FAILURE: Test suite is failing on main — fix before running fix-issue.
   [test output excerpt]
   ```

### Detect Project Type

Determine `AGENT_TYPE` for the build agent:
- If `package.json` with `bun` or `typescript` → `backend-typescript-architect`
- If `pyproject.toml` or `requirements.txt` → `python-backend-engineer`
- Otherwise → `general-purpose`

### Notification: Fix Started

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Validating issue" --icon hammer --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.09 --label "Phase 1: Validate"'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "Fix Issue Started" "#[ISSUE_NUMBER] — [ISSUE_TITLE]"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "🔧 Fix Issue Started" "#[ISSUE_NUMBER] — [ISSUE_TITLE]"'
```

Record `FIX_START_TIME` for duration tracking.

## Phase 2: Fetch Issue & Validate (DIRECT)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Fetching issue" --icon hammer --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.18 --label "Phase 2: Fetch issue"'
```

```bash
gh issue view $ISSUE_NUMBER --json number,title,body,state,assignees,labels
```

**Stop conditions** — STOP and report if:
- Issue is `closed`
- Issue is assigned to someone else (not the current `gh` user)
- Issue has label `wontfix` or `won't fix`

Extract: `ISSUE_NUMBER`, `ISSUE_TITLE`, `ISSUE_BODY`, `ISSUE_LABELS`.

## Phase 3: Investigation Agent (DISPATCHED — general-purpose, sonnet)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Investigating" --icon hammer --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.27 --label "Phase 3: Investigate"'
```

Launch a `general-purpose` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/investigation-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{ISSUE_BODY}}` → issue body text
- `{{ISSUE_LABELS}}` → comma-separated labels

The agent investigates the codebase, finds the root cause, and produces a fix plan.

Extract from the agent result:
- `ROOT_CAUSE`, `COMPLEXITY`, `FIX_APPROACH`, `FILES_TO_MODIFY`, `RISK`, `INVESTIGATION_STATUS`

If `INVESTIGATION_STATUS: BLOCKED` — log the reason, skip this issue, and continue to the next issue (if batch mode) or STOP (if single issue).

## Phase 4: Build Agent (DISPATCHED — dynamic AGENT_TYPE, opus)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Building fix" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.36 --label "Phase 4: Build"'
```

Launch an agent with `subagent_type` set to `AGENT_TYPE` (detected in Phase 1) and model: **opus**, with the prompt from `${CLAUDE_SKILL_DIR}/build-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{ISSUE_BODY}}` → issue body text
- `{{ROOT_CAUSE}}` → from investigation agent
- `{{FIX_APPROACH}}` → from investigation agent
- `{{FILES_TO_MODIFY}}` → from investigation agent
- `{{COMPLEXITY}}` → from investigation agent
- `{{SKIP_COVERAGE}}` → `true` if `--skip-coverage` set, `false` otherwise

**If `--skip-coverage`**: Extract `PR_NUMBER` and `PR_URL` from the agent result. Skip Phase 5.
**If coverage enabled** (default): Extract `BRANCH_NAME` and `BUILD_STATUS` from the agent result. Proceed to Phase 5.

## Phase 5: Coverage Gate (DISPATCHED — qa-expert, sonnet) — skip if `--skip-coverage`

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Coverage check" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.45 --label "Phase 5: Coverage"'
```

Launch a `qa-expert` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{BRANCH_NAME}}` → branch name from build agent result
- `{{COVERAGE_THRESHOLD}}` → value from `--coverage-threshold` flag (default: `90`)
- `{{SECURITY_SCAN}}` → `on` (default)

The agent runs coverage analysis, adds tests, pushes the branch, and creates the PR.

Extract `PR_NUMBER`, `PR_URL`, `COVERAGE_PCT`, `TESTS_ADDED`, `COVERAGE_STATUS`, and `SECURITY_STATUS` from the agent result.

If `COVERAGE_STATUS: WARN` — log a warning but continue.
If the coverage agent fails entirely — proceed to Phase 8 (bugfix loop).

## Phase 6: Review Gate (DISPATCHED — senior-code-reviewer, opus)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Code review" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.64 --label "Phase 6: Review"'
```

Launch a `senior-code-reviewer` agent (model: **opus**) with the prompt from `${CLAUDE_SKILL_DIR}/review-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number from Phase 4 (if `--skip-coverage`) or Phase 5 (default)

The agent reviews the PR, fixes issues if found, and approves when satisfied.

Extract `APPROVAL_STATUS`, `REVIEW_SUMMARY`, and `FIXES_APPLIED` from the agent result.

If `APPROVAL_STATUS: CHANGES_NEEDED` persists after the review agent's fixes, proceed to Phase 8 (bugfix loop).

## Phase 7: E2E Gate (DISPATCHED — qa-expert, sonnet) — skip unless `--e2e-gate=block|warn`

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "E2E testing" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.73 --label "Phase 7: E2E"'
```

Launch a `qa-expert` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/e2e-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number
- `{{BRANCH_NAME}}` → branch name

Handle result per `--e2e-gate` mode:
- `warn` + FAIL: log warning, continue to next phase
- PASS: continue

If FAIL with `--e2e-gate=warn`, log the failure and proceed (do not block).

## Phase 8: Bugfix Loop (on any gate failure — general-purpose, opus)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Bugfix loop" --icon hammer --color "#FF3B30"'
bash -c '~/.claude/hooks/cmux-bridge.sh log warning "Gate failure — entering bugfix loop" --source fix-issue'
```

Launch a `general-purpose` agent (model: **opus**) with the prompt from `${CLAUDE_SKILL_DIR}/bugfix-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{BRANCH_NAME}}` → the fix branch
- `{{FAILED_STEP}}` → which phase failed (build | coverage | review | e2e)
- `{{FAILURE_OUTPUT}}` → the error/failure text from the failed agent

Extract `FAILURE_CATEGORY`, `ISSUE_NUMBER` (sub-issue), `FIX_STATUS`, `TESTS_PASSING` from the agent result.

- If `FIX_STATUS: FIXED` and `TESTS_PASSING: true`:
  - Log: "Fixed ([FAILURE_CATEGORY]) — retrying from failed step"
  - **Re-run from the phase that failed** (not from Phase 4):
    - If build failed → re-run Phase 4
    - If coverage failed → re-run Phase 5
    - If review flagged issues → re-run Phase 6
    - If E2E failed → re-run Phase 7
  - Allow **max 2 bugfix iterations** to prevent infinite loops
- If `FIX_STATUS: UNFIXED`:
  - Log the failure details and skip this issue (continue to next issue in batch mode, or STOP if single issue)

## Phase 9: Merge Agent (DISPATCHED — general-purpose, haiku)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Merging" --icon hammer --color "#34C759"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.82 --label "Phase 9: Merge"'
```

Once all gates pass, launch a `general-purpose` agent (model: **haiku**) with the prompt from `${CLAUDE_SKILL_DIR}/merge-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number

The agent merges the PR, comments on and closes the issue, returns to main.

Extract `MERGE_STATUS` from the agent result.

If `MERGE_STATUS: CONFLICT` or `MERGE_STATUS: FAILED` — log error and STOP.

## Phase 10: Summary Agent (DISPATCHED — general-purpose, haiku)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Summarizing" --icon hammer --color "#34C759"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.91 --label "Phase 10: Summary"'
```

Launch a `general-purpose` agent (model: **haiku**) with the prompt from `${CLAUDE_SKILL_DIR}/summary-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number
- `{{PR_URL}}` → PR URL
- `{{BRANCH_NAME}}` → branch name
- `{{ROOT_CAUSE}}` → from investigation
- `{{FIX_APPROACH}}` → from investigation
- `{{COMPLEXITY}}` → from investigation
- `{{COVERAGE_PCT}}` → from coverage gate (or `N/A`)
- `{{COVERAGE_STATUS}}` → from coverage gate (or `skipped`)
- `{{TESTS_ADDED}}` → from coverage gate (or `0`)
- `{{SECURITY_STATUS}}` → from coverage gate (or `skipped`)
- `{{APPROVAL_STATUS}}` → from review gate
- `{{REVIEW_SUMMARY}}` → from review gate
- `{{FIXES_APPLIED}}` → from review gate
- `{{E2E_RESULT}}` → from E2E gate (or `skipped`)
- `{{E2E_SUMMARY}}` → from E2E gate (or `N/A`)
- `{{BUGFIX_ITERATIONS}}` → count of bugfix loop runs
- `{{FIX_START_TIME}}` → recorded start time

The agent produces a formatted markdown summary.

## Phase 11: Print & Notify (DIRECT)

Print the formatted summary returned by the summary agent.

### Notification: Fix Complete

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh progress 1.0 --label "Complete"'
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Complete" --icon sparkle --color "#34C759"'
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Fix complete: #[ISSUE_NUMBER] — [ISSUE_TITLE]" --source fix-issue'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "[EMOJI] Fix Issue Complete" "#[ISSUE_NUMBER] — [ISSUE_TITLE]\nPR: #[PR_NUMBER]\nDuration: [DURATION]"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "[EMOJI] Fix Issue Complete" "#[ISSUE_NUMBER] — [ISSUE_TITLE]\nPR: #[PR_NUMBER]\nDuration: [DURATION]"'
```

- Use `✅` if all gates passed cleanly, `⚠️` if any gate had warnings

### Batch Loop (if `next --limit=N` or `all`)

If the original target was `next --limit=N` or `all`/`opened issues`:
1. After completing (or skipping) the current issue, move to the next issue in the queue
2. Before each new issue: `git checkout main && git pull` to ensure clean state
3. If more issues remain: loop back to Phase 2 with the next issue
4. If no more issues: stop and print a batch summary of all issues processed (fixed / skipped / failed)

## Context Budget Rules

**NEVER read project source files directly** — the build and investigation agents read them.
Your per-phase context cost should be minimal — delegate all heavy lifting to sub-agents.

## Context

$ARGUMENTS
