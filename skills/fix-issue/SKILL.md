---
name: fix-issue
description: Fully autonomous issue fixer — thin orchestrator that delegates investigation, build, quality gates, merge, and summary to sub-agents.
user-invocable: true
disable-model-invocation: true
argument-hint: "<issue-number|issue-url|next> [--confirm] [--skip-coverage] [--e2e-gate=block|warn|off] [--skip-e2e] [--auto] [--limit=N] [--coverage-threshold=N] [--skip-preflight]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context.

## Phase 1: Parse Arguments & Validate Environment (DIRECT)

Parse `$ARGUMENTS` for:
- **Target**: issue number (e.g., `123`), issue URL (e.g., `https://github.com/owner/repo/issues/123`), or `next` (highest priority open bug)
- **Flags**:
  - `--confirm` — stop after investigation for user approval before building
  - `--skip-coverage` — bypass coverage gate (build agent creates PR directly)
  - `--e2e-gate=block|warn|off` — E2E test gate behavior (default: `off`)
  - `--skip-e2e` — shorthand for `--e2e-gate=off`
  - `--auto` — skip interactive prompts on failure
  - `--limit=N` — when using `next`, process up to N open bugs sequentially
  - `--coverage-threshold=N` — override the default 90% coverage threshold
  - `--skip-preflight` — skip the pre-flight health check

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

### Telegram Notification: Fix Started

```bash
bash -c 'source ~/.claude/config/.env 2>/dev/null && [ -n "$TELEGRAM_BOT_TOKEN" ] && curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -H "Content-Type: application/json" -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"🔧 *Fix Issue Started*\nIssue: #[ISSUE_NUMBER]\nTime: $(TZ=Europe/Paris date +"%Y-%m-%d %H:%M:%S CET")\", \"parse_mode\": \"Markdown\"}" > /dev/null'
```

If `TELEGRAM_BOT_TOKEN` is not set, skip silently — notifications are optional.

Record `FIX_START_TIME` for duration tracking.

## Phase 2: Fetch Issue & Validate (DIRECT)

```bash
gh issue view $ISSUE_NUMBER --json number,title,body,state,assignees,labels
```

**Stop conditions** — STOP and report if:
- Issue is `closed`
- Issue is assigned to someone else (not the current `gh` user)
- Issue has label `wontfix` or `won't fix`

Extract: `ISSUE_NUMBER`, `ISSUE_TITLE`, `ISSUE_BODY`, `ISSUE_LABELS`.

## Phase 3: Investigation Agent (DISPATCHED — general-purpose, sonnet)

Launch a `general-purpose` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/investigation-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{ISSUE_BODY}}` → issue body text
- `{{ISSUE_LABELS}}` → comma-separated labels

The agent investigates the codebase, finds the root cause, and produces a fix plan.

Extract from the agent result:
- `ROOT_CAUSE`, `COMPLEXITY`, `FIX_APPROACH`, `FILES_TO_MODIFY`, `RISK`, `INVESTIGATION_STATUS`

**If `--confirm` flag is set**: Print the investigation results as a structured plan and STOP for user approval before proceeding. Otherwise: continue autonomously.

If `INVESTIGATION_STATUS: BLOCKED` — print the reason and STOP.

## Phase 4: Build Agent (DISPATCHED — dynamic AGENT_TYPE, opus)

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

Launch a `senior-code-reviewer` agent (model: **opus**) with the prompt from `${CLAUDE_SKILL_DIR}/review-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number from Phase 4 (if `--skip-coverage`) or Phase 5 (default)

The agent reviews the PR, fixes issues if found, and approves when satisfied.

Extract `APPROVAL_STATUS`, `REVIEW_SUMMARY`, and `FIXES_APPLIED` from the agent result.

If `APPROVAL_STATUS: CHANGES_NEEDED` persists after the review agent's fixes, proceed to Phase 8 (bugfix loop).

## Phase 7: E2E Gate (DISPATCHED — qa-expert, sonnet) — skip unless `--e2e-gate=block|warn`

Launch a `qa-expert` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/e2e-gate-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number
- `{{BRANCH_NAME}}` → branch name

Handle result per `--e2e-gate` mode:
- `block` + FAIL: if `--auto` treat as `warn`, otherwise ask user: retry / continue / abort
- `warn` + FAIL: log warning, continue
- PASS: continue

If FAIL and not continuing, proceed to Phase 8 (bugfix loop).

## Phase 8: Bugfix Loop (on any gate failure — general-purpose, opus)

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
  - If `--auto`: log failure and STOP
  - Otherwise: ask user what to do

## Phase 9: Merge Agent (DISPATCHED — general-purpose, haiku)

Once all gates pass, launch a `general-purpose` agent (model: **haiku**) with the prompt from `${CLAUDE_SKILL_DIR}/merge-agent-prompt.md`, substituting:
- `{{ISSUE_NUMBER}}` → current issue number
- `{{ISSUE_TITLE}}` → current issue title
- `{{PR_NUMBER}}` → PR number

The agent merges the PR, comments on and closes the issue, returns to main.

Extract `MERGE_STATUS` from the agent result.

If `MERGE_STATUS: CONFLICT` or `MERGE_STATUS: FAILED` — log error and STOP.

## Phase 10: Summary Agent (DISPATCHED — general-purpose, haiku)

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

### Telegram Notification: Fix Complete

```bash
bash -c 'source ~/.claude/config/.env 2>/dev/null && [ -n "$TELEGRAM_BOT_TOKEN" ] && curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -H "Content-Type: application/json" -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"[EMOJI] *Fix Issue Complete*\nIssue: #[ISSUE_NUMBER] — [ISSUE_TITLE]\nPR: #[PR_NUMBER]\nDuration: [DURATION]\nTime: $(TZ=Europe/Paris date +"%Y-%m-%d %H:%M:%S CET")\", \"parse_mode\": \"Markdown\"}" > /dev/null'
```

- Use `✅` if all gates passed cleanly, `⚠️` if any gate had warnings
- If `TELEGRAM_BOT_TOKEN` is not set, skip silently

### Batch Loop (if `next --limit=N`)

If the original target was `next` and `--limit=N` was specified (N > 1):
1. Decrement remaining count
2. If remaining > 0: run `gh issue list --label bug --state open --limit 1 --json number,title -q '.[0]'` to get next open bug
3. If an issue is found: loop back to Phase 2 with the new issue
4. If no more issues: stop

## Context Budget Rules

**NEVER read project source files directly** — the build and investigation agents read them.
Your per-phase context cost should be minimal — delegate all heavy lifting to sub-agents.

## Context

$ARGUMENTS
