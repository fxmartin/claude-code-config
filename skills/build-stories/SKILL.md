---
name: build-stories
description: Batch build all incomplete stories across epics — thin dispatcher that delegates all heavy work to sub-agents for maximum context efficiency.
user-invocable: true
disable-model-invocation: true
argument-hint: "[all|resume|epic-NN|epic-name] [--dry-run] [--auto] [--skip-coverage]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context. This preserves your context window across 20+ story builds.

## Phase 1: Parse Arguments & Validate Environment (DIRECT)

Parse `$ARGUMENTS` for:
- **Scope**: `all` | `resume` | `epic-NN` (e.g., `epic-03`) | `<epic-name>` (e.g., `authentication`)
  - Default to `all` if no scope provided
- **Flags**:
  - `--dry-run` — show ordered build queue without executing
  - `--auto` — skip failed stories automatically (no interactive prompts)
  - `--e2e-gate=block|warn|off` — E2E test gate behavior between epics (default: `block`)
  - `--skip-e2e` — shorthand for `--e2e-gate=off`
  - `--skip-coverage` — bypass the coverage gate (build agent creates PR directly)

Run these 5 quick validation checks directly (too trivial to delegate):

```bash
# 1. Stories file exists
ls STORIES.md docs/STORIES.md 2>/dev/null
# 2. Clean git state
git status --porcelain
# 3. On main branch
git branch --show-current
# 4. Pull latest
git pull
# 5. GitHub CLI auth
gh auth status
```

Determine the progress file path: `docs/stories/.build-progress.md` (or `stories/.build-progress.md`).

## Phase 2: Dispatch Discovery Agent

Launch a `general-purpose` agent with the prompt from `${CLAUDE_SKILL_DIR}/discovery-agent-prompt.md`, substituting:
- `{{SCOPE}}` → parsed scope
- `{{E2E_GATE}}` → parsed e2e-gate mode
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`
- `{{PROGRESS_FILE}}` → resolved progress file path

The agent returns: a display table, blocked/completed lists, and a `QUEUE_JSON:` line.

## Phase 3: Parse Queue (DIRECT)

Extract the `QUEUE_JSON:` line from the discovery agent result. Parse it into an in-memory list of story records. Each record has: `id`, `title`, `epic_id`, `epic_name`, `epic_file`, `priority`, `points`, `agent_type`, `dependencies`.

If the agent returned `DISCOVERY_ERROR:` — print the error and STOP.
If the agent returned `RESUME_WARNING:` — print warning, ask user to confirm or use `resume`.

## Phase 4: Dry Run Check (DIRECT)

If `--dry-run` was specified:
1. Print the display table from the discovery agent (it's already formatted)
2. STOP — do not execute any builds

## Phase 5: Build Loop

Record batch start time. Initialize progress file if this is a fresh run (not resume).

**FOR EACH story in the queue:**

### Step 5a: Launch Build Agent

Use the Agent tool with `subagent_type` set to the story's `agent_type`. Include in the prompt:

**If `--skip-coverage` is set** (build agent handles push + PR):

```
You are building story [ID]: [TITLE]

Epic: [EPIC_NAME] (from [EPIC_FILE])
Priority: [PRIORITY]

## Pre-Step: Update Progress
Before starting development, update the progress file at [PROGRESS_FILE]:
- Read the file, find/add a row for story [ID]
- Set status to IN_PROGRESS, record start time and branch name feature/[ID]

## Instructions
1. Create branch: git checkout -b feature/[ID]
2. Read [EPIC_FILE] and find the full story section for [ID]
3. Follow TDD: write failing tests first, then implement
4. Run all quality gates (tests, types, lint, security)
5. Commit: feat([epic-name]): [story title] (#[ID])
6. Push and create PR:
   git push -u origin feature/[ID]
   gh pr create --title "feat: [Story Title] (#[ID])" --body "[summary, testing, Implements Story [ID]]"

Return PR_NUMBER: [number] and PR_URL: [url] when done.
```

Extract `PR_NUMBER` from the agent result. Skip Step 5a2.

**If coverage gate is enabled** (default — build agent commits locally only):

```
You are building story [ID]: [TITLE]

Epic: [EPIC_NAME] (from [EPIC_FILE])
Priority: [PRIORITY]

## Pre-Step: Update Progress
Before starting development, update the progress file at [PROGRESS_FILE]:
- Read the file, find/add a row for story [ID]
- Set status to IN_PROGRESS, record start time and branch name feature/[ID]

## Instructions
1. Create branch: git checkout -b feature/[ID]
2. Read [EPIC_FILE] and find the full story section for [ID]
3. Follow TDD: write failing tests first, then implement
4. Run all quality gates (tests, types, lint, security)
5. Commit locally: feat([epic-name]): [story title] (#[ID])
6. DO NOT push or create a PR — the coverage agent handles that next.

Return BRANCH_NAME: feature/[ID] and BUILD_STATUS: SUCCESS when done.
```

Extract `BRANCH_NAME` and `BUILD_STATUS` from the agent result. Proceed to Step 5a2.

### Step 5a2: Launch Coverage Gate Agent (skip if `--skip-coverage`)

Launch a `qa-expert` agent with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
- `{{STORY_ID}}` → current story ID
- `{{STORY_TITLE}}` → current story title
- `{{EPIC_NAME}}` → current epic name
- `{{EPIC_FILE}}` → story's epic file path
- `{{BRANCH_NAME}}` → branch name from build agent result

The agent runs coverage analysis, adds tests, pushes the branch, and creates the PR.

Extract `PR_NUMBER`, `PR_URL`, `COVERAGE_PCT`, `TESTS_ADDED`, and `COVERAGE_STATUS` from the agent result.

If `COVERAGE_STATUS: WARN` — log a warning but continue (coverage was best-effort).
If the coverage agent fails entirely — treat as a build failure (Step 5d error handling applies).

### Step 5b: Launch Review Agent

```
Agent(subagent_type="senior-code-reviewer", prompt="""
Review the PR for story [ID]: [TITLE]

1. gh pr view [PR_NUMBER]
2. gh pr diff [PR_NUMBER]
3. Check: architecture, security, performance, test coverage, code quality
4. If changes needed: checkout branch, fix, commit, push, re-review
5. When satisfied: gh pr review [PR_NUMBER] --approve

Return APPROVAL_STATUS: APPROVED or APPROVAL_STATUS: CHANGES_NEEDED
""")
```

If `APPROVAL_STATUS: CHANGES_NEEDED` persists after review agent's fixes, treat as FAILED.

### Step 5c: Launch Merge+Update Agent

> Note: The PR was created by either the build agent (`--skip-coverage`) or the coverage agent (default).

Launch a `general-purpose` agent with the prompt from `${CLAUDE_SKILL_DIR}/merge-update-prompt.md`, substituting:
- `{{STORY_ID}}` → current story ID
- `{{STORY_TITLE}}` → current story title
- `{{PR_NUMBER}}` → PR number from build agent (if `--skip-coverage`) or coverage agent (default)
- `{{EPIC_FILE}}` → story's epic file path
- `{{PROGRESS_FILE}}` → progress file path
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`

Parse the `MERGE_STATUS:` line from the result.

### Step 5d: Error Handling (DIRECT)

If build, coverage, review, or merge failed:
- If `--auto` flag: log failure, mark story FAILED in progress file, continue to next story
- If no `--auto`: ask user: **retry** (re-run from Step 5a), **skip** (mark SKIPPED, continue), or **abort** (save progress, stop)
- On abort: skip to Phase 6 for summary

### Step 5e: E2E Gate Check (DIRECT + conditional agent)

After each successful story, check if this was the last story for its epic in the queue:
- Compare `epic_id` of current story with remaining stories
- If no more stories from this `epic_id` remain AND `--e2e-gate` is not `off`:

Read `${CLAUDE_SKILL_DIR}/e2e-gate.md` for the full logic, then launch:

```
Agent(subagent_type="qa-expert", prompt="""
Epic [EPIC_ID]: [EPIC_NAME] — all stories built and merged.

[Include full prompt from e2e-gate.md with substitutions]

Return: E2E_RESULT: PASS or E2E_RESULT: FAIL with summary.
""")
```

Handle result per `--e2e-gate` mode:
- `block` + FAIL: if `--auto` treat as `warn`, otherwise ask user
- `warn` + FAIL: log warning, continue
- Record E2E gate result in progress file

## Phase 6: Dispatch Summary Agent

Launch a `general-purpose` agent with the prompt from `${CLAUDE_SKILL_DIR}/summary-prompt.md`, substituting:
- `{{PROGRESS_FILE}}` → progress file path
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`
- `{{BATCH_START}}` → recorded batch start time

## Phase 7: Print Report (DIRECT)

Print the formatted summary returned by the summary agent. Done.

## Context Budget Rules

**NEVER read epic files directly** — the discovery agent and build agents read them.
**NEVER read the progress file directly** — the merge-update and summary agents read it.
**NEVER read story-parser.md, dependency-resolver.md, or batch-progress.md** — agents read them.

Your per-story context cost should be ~100 tokens (story JSON record + control flow), not ~500+ tokens of file contents.

## Context

Project structure:
!`ls -d */ 2>/dev/null | head -20`

Existing stories:
!`ls docs/stories/epic-*.md 2>/dev/null || ls stories/epic-*.md 2>/dev/null || echo "No epic files found"`

Previous build progress:
!`cat docs/stories/.build-progress.md 2>/dev/null || cat stories/.build-progress.md 2>/dev/null || echo "No previous build session"`

$ARGUMENTS
