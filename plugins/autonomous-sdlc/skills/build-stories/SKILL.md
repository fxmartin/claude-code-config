---
name: build-stories
description: Batch build all incomplete stories across epics — thin dispatcher that delegates all heavy work to sub-agents for maximum context efficiency.
user-invocable: true
disable-model-invocation: true
argument-hint: "[all|resume|epic-NN|epic-name] [--dry-run] [--auto] [--skip-coverage] [--limit=N] [--sequential] [--coverage-threshold=N] [--skip-preflight]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

> **cmux environment check** — this skill emits cmux sidebar updates via `cmux-bridge.sh`. Before emitting any call whose subcommand is `status`, `progress`, `log`, or `clear`, check whether the `$CMUX_SOCKET_PATH` environment variable is set. If it is **empty** (running outside cmux — e.g. Claude Desktop App), **skip every such call in this skill**: they only drive the cmux sidebar UI and produce no effect elsewhere. Always run `cmux-bridge.sh notify` and `cmux-bridge.sh telegram` calls regardless of environment — they deliver to Telegram even when cmux is absent.

You are a **thin dispatcher** orchestrator. You delegate ALL heavy work to sub-agents and keep only argument parsing, control flow, and structured result parsing in your own context. This preserves your context window across 20+ story builds.

> **SQLite ledger (Epic-04, Story 4.2-001)** — alongside every `cmux-bridge log` call, also write to the durable ledger via `~/.claude/hooks/sdlc-state-emit.sh`. The orchestrator initialises the ledger and exports `SDLC_RUN_ID`; sub-agents inherit it and emit stage transitions. The markdown progress file (`.build-progress.md`) is still updated for human readability — story 4.2-002 will switch the markdown to a SELECT-only view. For now, BOTH write paths must run on every transition. The emit hook is silent (exit 0) when no ledger is configured, so legacy environments are not broken.

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
  - `--sequential` — disable parallel mode and build stories one at a time (default is parallel with git worktree isolation, max 5 concurrent agents per stage)
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

### Initialise SQLite Ledger (Story 4.2-001)

Create the durable ledger row for this run BEFORE the first sub-agent dispatch so every subsequent stage emission has a valid `SDLC_RUN_ID` to attach to:

```bash
# Initialise the ledger DB if it does not yet exist (idempotent).
~/.claude/hooks/sdlc-state-emit.sh init >/dev/null 2>&1 || \
  scripts/sdlc-state.sh init >/dev/null

# Create the run row; capture the ID and export it for child agents.
export SDLC_RUN_ID="$(~/.claude/hooks/sdlc-state-emit.sh run-create "${SCOPE:-all}" "${MODE:-parallel}")"
~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID}" "" info build-stories "run started: scope=${SCOPE:-all} mode=${MODE:-parallel}"
```

`SDLC_RUN_ID` is inherited by every dispatched sub-agent (Agent tool calls preserve env). Sub-agents append stage/event rows via the same hook. If the hook exits silently (no DB), the orchestrator continues — the markdown progress file is still updated.

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
~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID:-}" "" info build-stories "discovery complete: [N] stories across [M] epics"
```

On error:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Discovery FAILED" --icon x.circle --color "#FF3B30"'
bash -c '~/.claude/hooks/cmux-bridge.sh log error "Discovery failed: [reason]" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "Build Stories: Discovery Failed" "[reason]"'
rm -f /tmp/.claude-skill-active
```
Print the error and STOP. No Telegram — discovery failures are config issues, user is at keyboard.

## Phase 3: Parse Queue (DIRECT) — including Resume

Extract the `QUEUE_JSON:` line from the discovery agent result. Parse it into an in-memory list of story records. Each record has: `id`, `title`, `epic_id`, `epic_name`, `epic_file`, `priority`, `points`, `agent_type`, `dependencies`.

If the agent returned `DISCOVERY_ERROR:` — print the error and STOP.
If the agent returned `RESUME_WARNING:` — print warning, ask user to confirm or use `resume`.

### Resume Logic (cutover to SQLite ledger — Story 4.3-001)

When `scope=resume`, the ledger is the source of truth (the markdown
progress file is regenerated FROM the ledger by Story 4.2-002 and is no
longer authoritative for resume decisions).

Pre-resume sanity check (orchestrator):

```bash
# Ambiguity guard: if two IN_PROGRESS runs share the same started_at,
# bail out instead of silently picking one. The user can pass --run-id
# (a future flag) to disambiguate.
AMBIG=$(sqlite3 .sdlc-state.db "
    SELECT COUNT(*) FROM (
        SELECT started_at FROM runs
         WHERE status='IN_PROGRESS'
         GROUP BY started_at
        HAVING COUNT(*) > 1
    );" 2>/dev/null || echo 0)
if [ "${AMBIG}" -gt 0 ]; then
    echo "ABORT: ambiguous resume state, please specify --run-id"
    rm -f /tmp/.claude-skill-active
    exit 1
fi
```

The discovery agent now drives resume via `~/.claude/hooks/sdlc-state-emit.sh
latest-incomplete-run` and `resume-plan <run-id>` (see Step 3 in
`discovery-agent-prompt.md`). The resulting `QUEUE_JSON:` entries carry:

- `status` — `IN_PROGRESS`, `PENDING`, `BLOCKED`, `FAILED`, `SKIPPED`
- `resume_from` — the stage name to re-enter (`build`, `coverage`, `review`, `merge`, or `e2e`); `null` for fresh PENDING work
- `branch` — preserved from the prior attempt (do NOT recreate)
- `pr_number` — preserved from the prior attempt (the merge agent reuses this)

For each story in the parsed queue:

- `status=DONE` → already filtered by `resume-plan`; never appears
- `status=IN_PROGRESS` → call `mark-stages-stale <run> <story> <resume_from>` BEFORE re-dispatching the agent, then start the next attempt at `resume_from` with `attempt = previous_attempt + 1`
- `status=PENDING` → run normally (Phase 5)
- `status=BLOCKED` → keep BLOCKED (dependencies still open)
- `status=FAILED` → in `--auto` mode, mark `SKIPPED` and continue; otherwise prompt user (retry / skip / abort)
- `status=SKIPPED` → leave skipped (do not retry)

The `RESUME_META:` line emitted by `resume-plan` carries the previous run's
`run_id`, `scope`, and `mode` — export `SDLC_RUN_ID=<resumed-id>` so the
existing event/stage rows accumulate under the same run instead of creating
a new one.

Skip-flag persistence: the run-level `events` table carries any
`--skip-coverage`/`--skip-e2e` flags emitted at run start (source=
`build-stories`, message starts with `flags:`). On resume, the orchestrator
must re-read these and pass them as-is so stages skipped intentionally on
the prior attempt stay skipped.

### Persist queue to ledger (Story 4.2-001)

For each parsed story, write a `stories` row so resume (4.3-001) can rebuild the queue from SQLite alone:

```bash
# For each story in the parsed queue:
~/.claude/hooks/sdlc-state-emit.sh story-upsert \
  "${SDLC_RUN_ID}" "${STORY_ID}" "${EPIC_ID}" "${TITLE}" \
  "${PRIORITY}" "${POINTS}" "${AGENT_TYPE}" "" "" TODO
```

The markdown progress file is still initialised below — both views coexist until 4.2-002 lands.

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

### Phase 4b: Parallel Worktree Scheduling (default, skip if `--sequential`)

Unless `--sequential` is set, organize the build queue into **dependency cohorts** and execute using **batch-per-stage parallelism with git worktree isolation**.

**Cohort computation:**
1. From the topologically sorted queue, identify stories whose dependencies are ALL already completed (or have no dependencies)
2. Group these into Cohort 1
3. Remove Cohort 1 from the queue, mark as "scheduled"
4. Repeat: find stories whose remaining dependencies are all in completed cohorts → Cohort 2, etc.

**Batch-per-stage execution model:**

Each cohort runs through 4 stages. Stages 1-3 are parallel (up to 5 concurrent agents). Stage 4 is sequential.

```
Cohort N:
  Stage 1: [build A, B, C, D, E] ← parallel, each in own worktree
  Stage 2: [coverage A, B, C, D, E] ← parallel, each in own worktree
  Stage 3: [review A, B, C, D, E] ← parallel, via gh CLI (no worktree)
  Stage 4: [merge A → merge B → merge C → merge D → merge E] ← sequential, with rebase-before-merge
```

**Why worktrees:** Each build/coverage agent gets an isolated copy of the repo via `isolation: "worktree"` on the Agent tool. This prevents file conflicts between concurrent agents working on different stories. Worktrees share the same `.git` directory, so branches created in one worktree are visible to others.

**Key rule — build agents ALWAYS push:** In parallel mode, even when coverage gate is enabled, the build agent must `git push -u origin feature/[ID]` before returning. This ensures the coverage agent (in a separate worktree) can `git fetch && git checkout feature/[ID]` to pick up the build work.

**Execution rules:**
- Launch up to **5 agents concurrently** per stage within a cohort
- Wait for ALL agents in a stage to complete before starting the next stage
- **Stage 4 (merge) is always sequential** with rebase-before-merge to prevent conflicts
- If any story fails in Stage 1-3, it is excluded from subsequent stages (marked FAILED, dependents become BLOCKED in future cohorts)
- If any merge in Stage 4 fails after rebase, route to bugfix agent (same as sequential mode)

**Progress tracking in parallel mode:**
- The orchestrator tracks progress centrally — agents do NOT update the progress file
- After each stage completes, the orchestrator updates the progress file for all stories in that batch
- This prevents race conditions from concurrent agents writing to the same file

**Workspace management for parallel cohorts:**

Before launching each cohort:

```bash
# Cohort-level status (overwrites `current`, no per-story pills)
bash -c '~/.claude/hooks/cmux-bridge.sh status current "Cohort [C]/[TC]: [K] stories" --icon layers --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Cohort [C] started: [story IDs] (worktree isolation)" --source build-stories'
```

Before each stage within a cohort:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "Cohort [C] Stage [S]: [stage-name] ([K] stories)" --icon [stage-icon] --color "[stage-color]"'
```

Stage icons and colors:
- Stage 1 (Build): icon `code`, color `#007AFF`
- Stage 2 (Coverage): icon `flask`, color `#5856D6`
- Stage 3 (Review): icon `magnifier`, color `#FF9500`
- Stage 4 (Merge): icon `merge`, color `#34C759`

After ALL stages for the cohort complete:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Cohort [C] complete: [passed]/[K] succeeded" --source build-stories'
# If any failed:
bash -c '~/.claude/hooks/cmux-bridge.sh log error "Cohort [C]: [ID] failed, dependents blocked: [list]" --source build-stories'
```

Progress in parallel mode is updated per-stage:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh progress [FRACTION] --label "Cohort [C]/[TC], Stage [S]/4"'
```

**Dry-run in parallel mode**: Show the cohort groupings in the display table with `--- Cohort N ---` separator rows and indicate worktree isolation will be used.

If `--sequential` is set, fall through to the standard sequential Phase 5.

## Phase 5: Build Loop

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Building" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.0 --label "Story 0/[TOTAL]"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Build started: [TOTAL] stories, scope=[SCOPE]" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "Build Stories Started" "Scope: [SCOPE]\nStories: [TOTAL]\nMode: [sequential|parallel]"'
```

Record batch start time. Initialize `stories_processed = 0` and `first_failure_sent = false`. Initialize progress file if this is a fresh run (not resume).

---

### Phase 5 — Parallel Worktree Mode (default)

In parallel mode (the default), execute each cohort using batch-per-stage parallelism. **FOR EACH cohort** computed in Phase 4b:

#### Parallel Stage 1: Build (concurrent, worktree-isolated)

Launch up to 5 build agents concurrently **in a single message with multiple Agent tool calls**, each with `isolation: "worktree"`:

```
Agent(
  subagent_type=[story.agent_type],
  model="opus",
  isolation="worktree",
  prompt="""
  You are building story [ID]: [TITLE]

  Epic: [EPIC_NAME] (from [EPIC_FILE])
  Priority: [PRIORITY]

  ## Instructions
  1. Create branch: git checkout -b feature/[ID]
  2. Read [EPIC_FILE] and find the full story section for [ID]
  3. Follow TDD: write failing tests first, then implement
  4. Run all quality gates (tests, types, lint, security)
  5. Commit: feat([epic-name]): [story title] (#[ID])
  6. PUSH the branch (required for parallel mode):
     git push -u origin feature/[ID]

  Return BRANCH_NAME: feature/[ID] and BUILD_STATUS: SUCCESS when done.
  If failed, return BUILD_STATUS: FAILED with error details.

  As the FINAL line of your response, emit a machine-readable result block that
  conforms to `controller/schemas/build-agent-response.schema.json`:

  <<<RESULT_JSON>>>
  {"branch_name": "feature/[ID]", "build_status": "SUCCESS", "commit_sha": "[SHA]"}
  <<<END_RESULT>>>

  (Set "build_status" to "FAILED" and add "error_summary" on failure. Optional
  "pr_number" when a PR was created.) The controller validates this block; a
  missing or malformed block is treated as a build failure.

  ## Sidebar Ledger + SQLite Ledger
  After each milestone, emit a structured log entry so the cmux sidebar shows parallel-agent progress. Only emit if $CMUX_SOCKET_PATH is set (same guard as the orchestrator preamble).

  bash -c '~/.claude/hooks/cmux-bridge.sh log info "BUILD_STARTED [ID]: [TITLE]" --source story-[ID]'
  # SQLite ledger (Story 4.2-001): mark the build stage IN_PROGRESS. SDLC_RUN_ID is inherited from the orchestrator.
  ~/.claude/hooks/sdlc-state-emit.sh stage-start "${SDLC_RUN_ID:-}" "[ID]" build 1

  # After git checkout -b succeeds:
  bash -c '~/.claude/hooks/cmux-bridge.sh log info "BRANCH_CREATED [ID]: feature/[ID]" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh story-upsert "${SDLC_RUN_ID:-}" "[ID]" "[EPIC_ID]" "[TITLE]" "[PRIORITY]" "[POINTS]" "[AGENT_TYPE]" "feature/[ID]" "" IN_PROGRESS

  # After all quality gates pass:
  bash -c '~/.claude/hooks/cmux-bridge.sh log success "TESTS_GREEN [ID]: all gates passed" --source story-[ID]'
  # After git push succeeds:
  bash -c '~/.claude/hooks/cmux-bridge.sh log success "BRANCH_PUSHED [ID]: feature/[ID] pushed" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" build 1 DONE "" ""

  # On failure:
  bash -c '~/.claude/hooks/cmux-bridge.sh log error "BUILD_FAILED [ID]: [error summary]" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" build 1 FAILED "build-error" ""
  ~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID:-}" "[ID]" error story-[ID] "build failed: [error summary]"
  """
)
```

After all build agents return, collect results. For each `BUILD_STATUS: FAILED`, mark story FAILED and exclude from subsequent stages. Update progress file centrally:
- Mark successful builds as IN_PROGRESS (branch pushed)
- Mark failed builds as FAILED

#### Parallel Stage 2: Coverage Gate (concurrent, worktree-isolated)

Skip if `--skip-coverage`. For each successfully built story, launch coverage agents concurrently with `isolation: "worktree"`:

```
Agent(
  subagent_type="qa-engineer",
  model="sonnet",
  isolation="worktree",
  prompt="""
  [coverage-gate-prompt.md with substitutions]
  NOTE: The branch {{BRANCH_NAME}} has been pushed to remote. Start by fetching and checking it out:
    git fetch origin
    git checkout {{BRANCH_NAME}}
  Then proceed with coverage analysis.

  ## Sidebar Ledger + SQLite Ledger
  Emit structured log entries at each milestone. Only emit if $CMUX_SOCKET_PATH is set.

  bash -c '~/.claude/hooks/cmux-bridge.sh log info "COVERAGE_STARTED {{STORY_ID}}: {{STORY_TITLE}}" --source story-{{STORY_ID}}'
  ~/.claude/hooks/sdlc-state-emit.sh stage-start "${SDLC_RUN_ID:-}" "{{STORY_ID}}" coverage 1

  # After running the test suite:
  bash -c '~/.claude/hooks/cmux-bridge.sh log info "COVERAGE_MEASURED {{STORY_ID}}: [PCT]% current" --source story-{{STORY_ID}}'
  # After adding gap-filling tests:
  bash -c '~/.claude/hooks/cmux-bridge.sh log info "TESTS_ADDED {{STORY_ID}}: [N] tests, [PCT]% coverage" --source story-{{STORY_ID}}'
  # After security scan:
  bash -c '~/.claude/hooks/cmux-bridge.sh log info "SECURITY_SCANNED {{STORY_ID}}: [CLEAN|WARN|BLOCK|SKIPPED]" --source story-{{STORY_ID}}'
  # After gh pr create:
  bash -c '~/.claude/hooks/cmux-bridge.sh log success "PR_CREATED {{STORY_ID}}: PR #[PR_NUMBER]" --source story-{{STORY_ID}}'
  ~/.claude/hooks/sdlc-state-emit.sh story-upsert "${SDLC_RUN_ID:-}" "{{STORY_ID}}" "" "{{STORY_TITLE}}" "" "" "" "{{BRANCH_NAME}}" "[PR_NUMBER]" IN_PROGRESS
  # Final status:
  bash -c '~/.claude/hooks/cmux-bridge.sh log success "COVERAGE_DONE {{STORY_ID}}: [PASS|WARN] [PCT]%" --source story-{{STORY_ID}}'
  ~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "{{STORY_ID}}" coverage 1 DONE "" ""
  """
)
```

Each coverage agent fetches the story's branch (pushed in Stage 1), adds tests, pushes, and creates the PR. Collect PR_NUMBER and COVERAGE_PCT from all agents.

If `--skip-coverage`: The build agents already pushed branches. Launch PR creation in parallel (or combine into Stage 3).

#### Parallel Stage 3: Review (concurrent, no worktree needed)

Launch review agents concurrently **in a single message** (no `isolation` needed — reviews use `gh` CLI only):

```
Agent(
  subagent_type="senior-code-reviewer",
  model="opus",
  prompt="""
  Review the PR for story [ID]: [TITLE]
  1. gh pr view [PR_NUMBER]
  2. gh pr diff [PR_NUMBER]
  3. Check: architecture, security, performance, test coverage, code quality
  4. If changes needed: checkout branch, fix, commit, push, re-review
  5. When satisfied: gh pr review [PR_NUMBER] --approve
  Return APPROVAL_STATUS: APPROVED or APPROVAL_STATUS: CHANGES_NEEDED

  As the FINAL line of your response, emit a machine-readable result block that
  conforms to `controller/schemas/review-agent-response.schema.json`:

  <<<RESULT_JSON>>>
  {"pr_number": [PR_NUMBER], "approval_status": "APPROVED", "change_count": 0, "final_status": "APPROVED"}
  <<<END_RESULT>>>

  (Use "CHANGES_NEEDED"/"REJECTED" when the PR is not approved; "change_count"
  is the number of changes you requested or applied.)

  ## Sidebar Ledger + SQLite Ledger
  Emit structured log entries at each milestone. Only emit if $CMUX_SOCKET_PATH is set.

  bash -c '~/.claude/hooks/cmux-bridge.sh log info "REVIEW_STARTED [ID]: [TITLE]" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh stage-start "${SDLC_RUN_ID:-}" "[ID]" review 1

  # If changes are needed and applied:
  bash -c '~/.claude/hooks/cmux-bridge.sh log warning "CHANGES_REQUESTED [ID]: fixes applied, re-reviewing" --source story-[ID]'
  # After gh pr review --approve:
  bash -c '~/.claude/hooks/cmux-bridge.sh log success "APPROVED [ID]: PR #[PR_NUMBER] approved" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" review 1 DONE "" ""
  # If CHANGES_NEEDED persists:
  bash -c '~/.claude/hooks/cmux-bridge.sh log error "REVIEW_FAILED [ID]: changes still needed" --source story-[ID]'
  ~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" review 1 FAILED "review-blocked" ""
  """
)
```

Collect results. Stories with `CHANGES_NEEDED` that persisted after the review agent's fixes are marked FAILED.

#### Parallel Stage 4: Merge (SEQUENTIAL, with rebase-before-merge)

For each approved story **one at a time, in topological order**:

Launch the merge agent with the prompt from `${CLAUDE_SKILL_DIR}/merge-update-prompt.md`. The merge agent now includes a **rebase-before-merge step** to handle baseline drift from earlier merges in this stage (see updated merge-update-prompt.md).

After each merge:
- Update progress file centrally: mark story DONE, record PR number and completion time
- Increment `stories_processed`
- Log success/failure per Step 5c2

If merge fails (conflict after rebase):
- Route to bugfix agent (Step 5d applies)
- If bugfix resolves it, retry the merge
- If not, mark FAILED, continue to next story

After all stories in the cohort are processed, proceed to next cohort.

**Error handling in parallel mode:**
- Build failures (Stage 1): Mark FAILED immediately, exclude from Stages 2-4
- Coverage failures (Stage 2): Route to bugfix agent per Step 5d (one at a time)
- Review failures (Stage 3): Mark FAILED, exclude from Stage 4
- Merge failures (Stage 4): Rebase + retry, then bugfix agent if needed
- Telegram alert: sent on first failure across all stages (same `first_failure_sent` gate)

---

### Phase 5 — Sequential Mode (only if `--sequential`)

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

As the FINAL line of your response, emit a machine-readable result block that
conforms to `controller/schemas/build-agent-response.schema.json` (this
skip-coverage build agent creates the PR, so include "pr_number"):

<<<RESULT_JSON>>>
{"branch_name": "feature/[ID]", "build_status": "SUCCESS", "commit_sha": "[SHA]", "pr_number": [PR_NUMBER]}
<<<END_RESULT>>>
```

Extract `PR_NUMBER` from the agent result. Skip Step 5a2.

**If coverage gate is enabled** (default — build agent commits locally only in sequential mode):

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

As the FINAL line of your response, emit a machine-readable result block that
conforms to `controller/schemas/build-agent-response.schema.json`:

<<<RESULT_JSON>>>
{"branch_name": "feature/[ID]", "build_status": "SUCCESS", "commit_sha": "[SHA]"}
<<<END_RESULT>>>

(Set "build_status" to "FAILED" and add "error_summary" on failure.)
```

> **Note:** In parallel mode (default), the build agent prompt differs — see Phase 5 Parallel Worktree Mode above. The parallel build agent always pushes the branch so that the coverage agent (in a separate worktree) can access it.

Extract `BRANCH_NAME` and `BUILD_STATUS` from the agent result. Proceed to Step 5a2.

### Step 5a2: Launch Coverage Gate Agent (skip if `--skip-coverage`)

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status current "[ID] Coverage" --icon flask --color "#5856D6"'
```

Launch a `qa-engineer` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
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

## Sidebar Ledger + SQLite Ledger
Emit structured log entries at each milestone. Only emit if $CMUX_SOCKET_PATH is set.

bash -c '~/.claude/hooks/cmux-bridge.sh log info "REVIEW_STARTED [ID]: [TITLE]" --source story-[ID]'
~/.claude/hooks/sdlc-state-emit.sh stage-start "${SDLC_RUN_ID:-}" "[ID]" review 1

# If changes are needed and applied:
bash -c '~/.claude/hooks/cmux-bridge.sh log warning "CHANGES_REQUESTED [ID]: fixes applied, re-reviewing" --source story-[ID]'
# After gh pr review --approve:
bash -c '~/.claude/hooks/cmux-bridge.sh log success "APPROVED [ID]: PR #[PR_NUMBER] approved" --source story-[ID]'
~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" review 1 DONE "" ""
# If CHANGES_NEEDED persists:
bash -c '~/.claude/hooks/cmux-bridge.sh log error "REVIEW_FAILED [ID]: changes still needed" --source story-[ID]'
~/.claude/hooks/sdlc-state-emit.sh stage-finish "${SDLC_RUN_ID:-}" "[ID]" review 1 FAILED "review-blocked" ""
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
  # Close the ledger run as ABORTED (Story 4.2-001).
  ~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID}" "" error build-stories "aborted by user at story [N]/[TOTAL]"
  ~/.claude/hooks/sdlc-state-emit.sh run-update-status "${SDLC_RUN_ID}" ABORTED
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
Agent(subagent_type="qa-engineer", model="sonnet", prompt="""
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
# SQLite ledger (Story 4.2-001): the schema has no dedicated e2e_gate table,
# so gate results are written into `events` with source=e2e-gate so the
# resume + summary flows can find them by SELECT ... WHERE source='e2e-gate'.
~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID:-}" "" success e2e-gate "E2E_PASS epic [EPIC_ID]: [EPIC_NAME]"
```

**E2E FAIL:**
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log error "E2E gate: epic [EPIC_ID] FAILED" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "E2E Gate FAILED" "Epic [EPIC_ID]: [EPIC_NAME]"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "E2E Gate Failed" "Epic [EPIC_ID]: [EPIC_NAME]\nMode: [block|warn]"'
~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID:-}" "" error e2e-gate "E2E_FAIL epic [EPIC_ID]: [EPIC_NAME] mode=[block|warn]"
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

## Phase 6b: Dispatch Documentation Update Agent

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status phase "Updating Docs" --icon pencil --color "#5856D6"'
bash -c '~/.claude/hooks/cmux-bridge.sh status current "README + story docs" --icon doc --color "#5856D6"'
```

If at least one story completed successfully, launch a `general-purpose` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/doc-update-prompt.md`, substituting:
- `{{PROGRESS_FILE}}` → progress file path
- `{{SCOPE}}` → parsed scope
- `{{COMPLETED_STORIES}}` → comma-separated list of completed story IDs and titles from this run
- `{{COMPLETED_PRS}}` → comma-separated list of PR numbers merged in this run

The agent reviews what changed across all merged stories and updates documentation in a single pass, avoiding the per-merge overhead.

On success:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Documentation updated for [N] merged stories" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
```

On failure (non-blocking — doc update failures should not fail the build):
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log warning "Documentation update failed — manual review needed" --source build-stories'
bash -c '~/.claude/hooks/cmux-bridge.sh clear current'
```

If no stories completed successfully, skip this phase entirely.

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
# Close out the ledger run (Story 4.2-001). Terminal status — DONE if every
# story completed, FAILED if any did, ABORTED on user abort.
~/.claude/hooks/sdlc-state-emit.sh event-log "${SDLC_RUN_ID}" "" success build-stories "run finished: [COMPLETED] done, [FAILED] failed"
~/.claude/hooks/sdlc-state-emit.sh run-update-status "${SDLC_RUN_ID}" "${RUN_TERMINAL_STATUS:-DONE}"
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
