---
name: build-stories
description: Batch build all incomplete stories across epics — thin dispatcher that delegates all heavy work to sub-agents for maximum context efficiency.
user-invocable: true
disable-model-invocation: true
argument-hint: "[all|resume|epic-NN|epic-name] [--dry-run] [--auto] [--skip-coverage] [--limit=N] [--parallel] [--coverage-threshold=N] [--skip-preflight]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context. This preserves your context window across 20+ story builds.

## Sidebar Notification Architecture

Use exactly **2 status pill keys** throughout the entire run. Never create per-story keyed pills.

| Key | Purpose | Lifecycle |
|-----|---------|-----------|
| `phase` | Current macro-phase (Preflight, Discovery, Building, Summarizing, Complete) | Overwritten at each phase transition |
| `current` | Current sub-step (story ID + step name) | Overwritten at each sub-step, cleared at finish |

**Notification channels:**
- **`status`**: Real-time sidebar visibility (every phase/step transition)
- **`progress`**: Global progress bar (updated per story)
- **`log`**: Permanent sidebar ledger (every significant event)
- **`notify`** (desktop): Milestones only — preflight failure, E2E gates, abort, finish
- **`telegram`**: Envelope only — start, first failure, E2E failure, abort, finish

**Orchestrator variables** (track across build loop):
- `stories_processed` (counter) — for progress fraction
- `first_failure_sent` (boolean) — gate Telegram to one failure alert per run

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
  - `--limit=N` — build at most N stories from the queue (useful for incremental runs)
  - `--parallel` — enable parallel story builds within dependency cohorts (max 3 concurrent agents)
  - `--coverage-threshold=N` — override the default 90% coverage threshold (e.g., `--coverage-threshold=80`)
  - `--skip-preflight` — skip the pre-flight health check (Step 1b)

Run these 5 quick validation checks directly (too trivial to delegate):

```bash
# 1. Stories file exists (use || true — ls returns non-zero if any path is missing)
ls STORIES.md docs/STORIES.md 2>/dev/null || true
# 2. Clean git state
git status --porcelain
# 3. On main branch
git branch --show-current
# 4. Pull latest
git pull
# 5. GitHub CLI auth
gh auth status 2>&1
```

Determine the progress file path: `docs/stories/.build-progress.md` (or `stories/.build-progress.md`).

### Activate Skill Sentinel

Write a sentinel file so cmux hooks suppress noisy behavior (agent pills, desktop notifications, progress clearing) while the skill manages its own sidebar:

```bash
echo "build-stories" > /tmp/.claude-skill-active
```

**Important:** This file MUST be removed on every exit path (finish, abort, preflight failure, discovery failure). See Phase 7 and error paths below.

### Pre-Flight Health Check (skip if `--skip-preflight`)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Preflight" --icon shield --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Preflight: validating environment" --source build-stories'
```

Before dispatching any agents, verify the project's test suite is green on main:

1. **Detect test command**: Check for test scripts in order of preference:
   - `package.json` → `scripts.test` → run `npm test`
   - `pyproject.toml` → detect pytest → run `uv run pytest`
   - `Makefile` → check for `test` target → run `make test`
   - `bats` test files → run `bats test/`

2. **Run tests** with sidebar visibility:
   ```bash
   bash -c '~/.claude/hooks/cmux-bridge.sh status current "Running test suite" --icon flask --color "#FF9500"'
   bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.0 --label "Preflight: test suite"'
   ```
   Execute the detected test command.

3. **On success**:
   ```bash
   bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
   bash -c '~/.claude/hooks/cmux-bridge.sh log success "Preflight passed" --source build-stories'
   ```

4. **Fail-fast if red**: If tests fail on main before any stories are built:
   ```bash
   bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Preflight FAILED" --icon x.circle --color "#FF3B30"'
   bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
   bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.0 --label "Preflight failed"'
   bash -c '~/.claude/hooks/cmux-bridge.sh log error "Preflight failed: test suite red on main" --source build-stories'
   bash -c '~/.claude/hooks/cmux-bridge.sh notify "Build Stories: Preflight Failed" "Test suite is failing on main. Fix before running."'
   bash -c '~/.claude/hooks/cmux-bridge.sh telegram "Build Stories: Preflight Failed" "Test suite is red on main. Cannot proceed."'
   rm -f /tmp/.claude-skill-active
   ```
   STOP with: `PRE_FLIGHT_FAILURE: Test suite is failing on main — fix before running build-stories.`

## Phase 2: Dispatch Discovery Agent

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Discovery" --icon search --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Discovery agent launched" --source build-stories'
```

Launch a `general-purpose` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/discovery-agent-prompt.md`, substituting:
- `{{SCOPE}}` → parsed scope
- `{{E2E_GATE}}` → parsed e2e-gate mode
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`
- `{{PROGRESS_FILE}}` → resolved progress file path

The agent returns: a display table, blocked/completed lists, and a `QUEUE_JSON:` line.

On success:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Discovered [N] stories across [M] epics" --source build-stories'
```

On error:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Discovery FAILED" --icon x.circle --color "#FF3B30"'
bash -c '~/.claude/hooks/cmux-bridge.sh log error "Discovery failed: [reason]" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "Build Stories: Discovery Failed" "[reason]"'
rm -f /tmp/.claude-skill-active
```
Print the error and STOP. No Telegram — discovery failures are config issues, user is at keyboard.

## Phase 3: Parse Queue (DIRECT)

Extract the `QUEUE_JSON:` line from the discovery agent result. Parse it into an in-memory list of story records. Each record has: `id`, `title`, `epic_id`, `epic_name`, `epic_file`, `priority`, `points`, `agent_type`, `dependencies`.

If the agent returned `DISCOVERY_ERROR:` — print the error and STOP.
If the agent returned `RESUME_WARNING:` — print warning, ask user to confirm or use `resume`.

### Queue Truncation (`--limit=N`)

If `--limit=N` was specified, truncate the parsed queue to the first N entries. Log:
```
Queue truncated to N stories (--limit=N). Remaining stories deferred to next run.
```
Dependency integrity: if truncation would split a dependency pair (story A depends on B, A is included but B is not), include B as well (even if it exceeds the limit by 1).

## Phase 4: Dry Run Check & Parallel Mode Setup (DIRECT)

If `--dry-run` was specified:
1. Print the display table from the discovery agent (it's already formatted)
2. Notify and STOP:
   ```bash
   bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Dry Run" --icon eye --color "#5856D6"'
   bash -c '~/.claude/hooks/cmux-bridge.sh log info "Dry run: [N] stories queued (not building)" --source build-stories'
   bash -c '~/.claude/hooks/cmux-bridge.sh clear phase'
   rm -f /tmp/.claude-skill-active
   ```

### Phase 4b: Parallel Scheduling (if `--parallel`)

When `--parallel` is enabled, organize the build queue into **dependency cohorts** — groups of stories that can be built concurrently because they share no dependencies between them.

**Cohort computation:**
1. From the topologically sorted queue, identify stories whose dependencies are ALL already completed (or have no dependencies)
2. Group these into Cohort 1
3. Remove Cohort 1 from the queue, mark as "scheduled"
4. Repeat: find stories whose remaining dependencies are all in completed cohorts → Cohort 2, etc.

**Execution rules:**
- Launch up to **3 build agents concurrently** per cohort (agent tool supports parallel calls)
- Wait for ALL agents in a cohort to complete before starting the next cohort
- **Post-build steps are always sequential**: coverage gate → review → merge happen one story at a time, even in parallel mode (to avoid merge conflicts)
- If any story in a cohort fails, remaining cohorts still execute (failed story is marked FAILED, dependents become BLOCKED)

**Workspace management for parallel cohorts:**

Before launching each cohort:

```bash
# Cohort-level status (overwrites `current`, no per-story pills)
bash -c '~/.claude/hooks/cmux-bridge.sh status current "Cohort [C]/[TC]: [K] stories" --icon layers --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Cohort [C] started: [story IDs]" --source build-stories'

# Split panes for concurrent agents (up to 3):
# First agent uses the current pane. Additional agents get new splits.
SURFACE_2=$(bash -c '~/.claude/hooks/cmux-bridge.sh pane-create "Story [STORY_2_ID]" right')
SURFACE_3=$(bash -c '~/.claude/hooks/cmux-bridge.sh pane-create "Story [STORY_3_ID]" down')
```

After ALL agents in the cohort complete:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh pane-close [SURFACE_2]'
bash -c '~/.claude/hooks/cmux-bridge.sh pane-close [SURFACE_3]'
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Cohort [C] complete: [passed]/[K] succeeded" --source build-stories'
# If any failed:
bash -c '~/.claude/hooks/cmux-bridge.sh log error "Cohort [C]: [ID] failed, dependents blocked: [list]" --source build-stories'
```

Progress in parallel mode is updated per-cohort:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh progress [FRACTION] --label "Cohort [C]/[TC], Story [N]/[TOTAL]"'
```

Capture the `surface:N` refs returned by `pane-create` so they can be closed after the cohort finishes. If `pane-create` returns empty (cmux unavailable), skip pane management silently.

**Dry-run with `--parallel`**: Show the cohort groupings in the display table with `--- Cohort N ---` separator rows.

If `--parallel` is NOT set, fall through to the standard sequential Phase 5.

## Phase 5: Build Loop

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Building" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.0 --label "Story 0/[TOTAL]"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Build started: [TOTAL] stories, scope=[SCOPE]" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "Build Stories Started" "Scope: [SCOPE]\nStories: [TOTAL]\nMode: [sequential|parallel]"'
```

Record batch start time. Initialize `stories_processed = 0` and `first_failure_sent = false`. Initialize progress file if this is a fresh run (not resume).

**FOR EACH story in the queue:**

### Step 5a: Launch Build Agent

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Building" --icon code --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress [FRACTION] --label "Story [N]/[TOTAL]: [ID]"'
```

Use the Agent tool with `subagent_type` set to the story's `agent_type` and model: **opus**. Include in the prompt:

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

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Coverage" --icon flask --color "#5856D6"'
```

Launch a `qa-expert` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
- `{{STORY_ID}}` → current story ID
- `{{STORY_TITLE}}` → current story title
- `{{EPIC_NAME}}` → current epic name
- `{{EPIC_FILE}}` → story's epic file path
- `{{BRANCH_NAME}}` → branch name from build agent result
- `{{COVERAGE_THRESHOLD}}` → value from `--coverage-threshold` flag (default: `90`)
- `{{SECURITY_SCAN}}` → `on` (default) or `off` if security scanning is not desired

The agent runs coverage analysis, adds tests, pushes the branch, and creates the PR.

Extract `PR_NUMBER`, `PR_URL`, `COVERAGE_PCT`, `TESTS_ADDED`, `COVERAGE_STATUS`, and `SECURITY_STATUS` from the agent result.

If `COVERAGE_STATUS: WARN` — log a warning but continue (coverage was best-effort).
If the coverage agent fails entirely — treat as a build failure (Step 5d error handling applies).

### Step 5b: Launch Review Agent

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Review" --icon magnifier --color "#FF9500"'
```

```
Agent(subagent_type="senior-code-reviewer", model="opus", prompt="""
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

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Merging" --icon merge --color "#34C759"'
```

Launch a `general-purpose` agent (model: **haiku**) with the prompt from `${CLAUDE_SKILL_DIR}/merge-update-prompt.md`, substituting:
- `{{STORY_ID}}` → current story ID
- `{{STORY_TITLE}}` → current story title
- `{{PR_NUMBER}}` → PR number from build agent (if `--skip-coverage`) or coverage agent (default)
- `{{EPIC_FILE}}` → story's epic file path
- `{{PROGRESS_FILE}}` → progress file path
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`

Parse the `MERGE_STATUS:` line from the result.

### Step 5c2: Per-Story Completion

After a story completes, increment `stories_processed` and update the sidebar log:

**On success:**
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log success "[N]/[TOTAL] [ID] [TITLE] -- PR #[PR]" --source build-stories'
```

**On failure:**
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] FAILED" --icon x.circle --color "#FF3B30"'
bash -c '~/.claude/hooks/cmux-bridge.sh log error "[N]/[TOTAL] [ID] [TITLE] -- [step] failed" --source build-stories'
```

No per-story desktop notifications. No per-story Telegram.

### Step 5d: Error Handling & Bugfix Loop (DIRECT + conditional agent)

If build, coverage, review, or merge failed:

**Step 5d1: Detect test/code failure**

Inspect the failure output from the failed step. If the failure involves **test assertions, runtime errors, or code behavior** — proceed to Step 5d2 for triage.

If the failure is clearly **infrastructure** (git error, network timeout, auth failure, tool crash) — skip to Step 5d3 (standard error handling).

**Step 5d2: Launch Bugfix Agent**

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Bugfix [1/2]" --icon wrench --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh log warning "[ID] Bugfix attempt [1/2]: [FAILURE_CATEGORY]" --source build-stories'
```

Launch a `general-purpose` agent (model: **opus**) with the prompt from `${CLAUDE_SKILL_DIR}/bugfix-agent-prompt.md`, substituting:
- `{{STORY_ID}}` → current story ID
- `{{STORY_TITLE}}` → current story title
- `{{EPIC_NAME}}` → current epic name
- `{{EPIC_FILE}}` → story's epic file path
- `{{BRANCH_NAME}}` → the story's branch (`feature/[ID]`)
- `{{FAILED_STEP}}` → which step failed (build | coverage | e2e)
- `{{FAILURE_OUTPUT}}` → the error/failure text from the failed agent

The agent classifies the failure as **CODE_BUG**, **TEST_BUG**, or **ENV_ISSUE**:
- **CODE_BUG**: creates a GH issue → fixes the code → retests → closes the issue if fixed
- **TEST_BUG**: fixes the test directly (no GH issue)
- **ENV_ISSUE**: reports the issue, attempts to fix if possible (no GH issue)

Extract `FAILURE_CATEGORY`, `ISSUE_NUMBER`, `FIX_STATUS`, `TESTS_PASSING`, `BUGS_FIXED`, and `TESTS_FIXED` from the agent result.

- If `FIX_STATUS: FIXED` and `TESTS_PASSING: true`:
  - ```bash
    bash -c '~/.claude/hooks/cmux-bridge.sh log success "[ID] Bugfix resolved: [FAILURE_CATEGORY], retrying from [step]" --source build-stories'
    ```
  - If `ISSUE_NUMBER` is set: log "GH issue #[ISSUE_NUMBER] closed"
  - **Re-run from the step that failed** (not from Step 5a):
    - If build failed → re-run Step 5a
    - If coverage failed → re-run Step 5a2
    - If review flagged test issues → re-run Step 5b
  - Allow **max 2 bugfix iterations** per story to prevent infinite loops
- If `FIX_STATUS: UNFIXED`:
  - ```bash
    bash -c '~/.claude/hooks/cmux-bridge.sh log error "[ID] Bugfix exhausted after [N] attempts" --source build-stories'
    ```
  - If `ISSUE_NUMBER` is set: log "Bug #[ISSUE_NUMBER] could not be auto-fixed — issue left open"
  - Fall through to Step 5d3
- If `FIX_STATUS: N/A` (ENV_ISSUE that couldn't be resolved):
  - Fall through to Step 5d3

**Step 5d3: Standard error handling**

If `first_failure_sent` is false, send Telegram alert and flip to true:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "Build Stories: First Failure" "[ID] [TITLE]\nStep: [step]\nStory [N]/[TOTAL]\nBugfix: [attempted|skipped]"'
```

- If `--auto` flag: log failure, mark story FAILED in progress file, continue to next story
- If no `--auto`: ask user: **retry** (re-run from Step 5a), **skip** (mark SKIPPED, continue), or **abort** (save progress, stop)
- On abort:
  ```bash
  bash -c '~/.claude/hooks/cmux-bridge.sh status phase "ABORTED" --icon stop --color "#FF3B30"'
  bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
  bash -c '~/.claude/hooks/cmux-bridge.sh progress [FRACTION] --label "Aborted at story [N]/[TOTAL]"'
  bash -c '~/.claude/hooks/cmux-bridge.sh log error "Build aborted by user at story [N]/[TOTAL]" --source build-stories'
  bash -c '~/.claude/hooks/cmux-bridge.sh notify "Build Stories Aborted" "Stopped at [N]/[TOTAL]\nCompleted: [C], Failed: [F]"'
  bash -c '~/.claude/hooks/cmux-bridge.sh telegram "Build Stories Aborted" "Stopped at story [N]/[TOTAL]\nCompleted: [C], Failed: [F]"'
  rm -f /tmp/.claude-skill-active
  ```
  Skip to Phase 6 for summary.

### Step 5e: E2E Gate Check (DIRECT + conditional agent)

After each successful story, check if this was the last story for its epic in the queue:
- Compare `epic_id` of current story with remaining stories
- If no more stories from this `epic_id` remain AND `--e2e-gate` is not `off`:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "E2E: [EPIC_NAME]" --icon flask --color "#5856D6"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "E2E gate: epic [EPIC_ID] -- all stories merged, running tests" --source build-stories'
```

Read `${CLAUDE_SKILL_DIR}/e2e-gate.md` for the full logic, then launch:

```
Agent(subagent_type="qa-expert", model="sonnet", prompt="""
Epic [EPIC_ID]: [EPIC_NAME] — all stories built and merged.

[Include full prompt from e2e-gate.md with substitutions]

Return: E2E_RESULT: PASS or E2E_RESULT: FAIL with summary.
""")
```

Handle result per `--e2e-gate` mode:

**E2E PASS:**
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log success "E2E gate: epic [EPIC_ID] PASSED" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "E2E Gate Passed" "Epic [EPIC_ID]: [EPIC_NAME]"'
```

**E2E FAIL:**
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log error "E2E gate: epic [EPIC_ID] FAILED" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "E2E Gate FAILED" "Epic [EPIC_ID]: [EPIC_NAME]"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "E2E Gate Failed" "Epic [EPIC_ID]: [EPIC_NAME]\nMode: [block|warn]"'
```

- `block` + FAIL: if `--auto` treat as `warn`, otherwise ask user
- `warn` + FAIL: log warning, continue
- Record E2E gate result in progress file

## Phase 6: Dispatch Summary Agent

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Summarizing" --icon doc --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
```

Launch a `general-purpose` agent (model: **haiku**) with the prompt from `${CLAUDE_SKILL_DIR}/summary-prompt.md`, substituting:
- `{{PROGRESS_FILE}}` → progress file path
- `{{CLAUDE_SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`
- `{{BATCH_START}}` → recorded batch start time

## Phase 7: Print Report & Notify (DIRECT)

Print the formatted summary returned by the summary agent.

### Notification: Build Finished

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Complete" --icon sparkle --color "#34C759"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 1.0 --label "Done: [COMPLETED]/[TOTAL]"'
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Build finished: [COMPLETED] done, [FAILED] failed, [SKIPPED] skipped, [DURATION]" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "[EMOJI] Build Stories Finished" "Scope: [SCOPE]\nDone: [COMPLETED]/[TOTAL]\nFailed: [FAILED]\nDuration: [DURATION]"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "[EMOJI] Build Stories Finished" "Scope: [SCOPE]\nDone: [COMPLETED]/[TOTAL]\nFailed: [FAILED]\nDuration: [DURATION]"'
rm -f /tmp/.claude-skill-active
```

Substitute `[COMPLETED]`, `[TOTAL]`, `[FAILED]`, `[SKIPPED]`, and `[DURATION]` from the summary agent results. If any stories failed, use `⚠️` instead of `✅` for `[EMOJI]`.

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
