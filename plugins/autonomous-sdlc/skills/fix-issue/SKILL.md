---
name: fix-issue
description: Fully autonomous issue fixer — thin orchestrator that delegates investigation, build, quality gates, merge, and summary to sub-agents.
user-invocable: true
disable-model-invocation: false
argument-hint: "<issue-number|issue-url|next|all> [--skip-coverage] [--e2e-gate=warn|off] [--skip-e2e] [--limit=N] [--coverage-threshold=N] [--skip-preflight] [--sequential]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

> **cmux environment check** — this skill emits cmux sidebar updates via `cmux-bridge.sh`. Before emitting any call whose subcommand is `status`, `progress`, `log`, or `clear`, check whether the `$CMUX_SOCKET_PATH` environment variable is set. If it is **empty** (running outside cmux — e.g. Claude Desktop App), **skip every such call in this skill**: they only drive the cmux sidebar UI and produce no effect elsewhere. Always run `cmux-bridge.sh notify` and `cmux-bridge.sh telegram` calls regardless of environment — they deliver to Telegram even when cmux is absent.

> **Dashboard run logging (Story 11.2-013)** — this skill mirrors its pipeline into the SDLC ledger + host registry so a `fix-issue` session shows up in `sdlc dashboard` beside `sdlc build` runs. It shells out to the minimal `sdlc run-open` / `sdlc run-stage` / `sdlc run-close` verbs (run from the repo root so they hit the repo's `.sdlc-state.db`). **All run-logging is strictly best-effort: every call is suffixed with `2>/dev/null || true`, and if `sdlc` is missing or `$RUN_ID` is empty, skip the rest — a logging failure must never block or fail the fix.**
>
> - **Open** (Phase 1, once the issue number is known): `RUN_ID=$(sdlc run-open --scope "issue-$ISSUE_NUMBER" --pid "$PPID" 2>/dev/null || true)`. Pass `--pid "$PPID"` (the long-lived orchestrator process) — **not** the default, which would be this short-lived `sdlc run-open` subprocess and would make the dashboard derive the still-running fix as `DEAD`. Reuse `$RUN_ID` for the rest of this issue. In batch mode each issue opens (and closes) its own run.
> - **Per phase** — at each dispatched phase's start emit `sdlc run-stage start --run "$RUN_ID" --stage <name> 2>/dev/null || true`, and at its end `sdlc run-stage finish --run "$RUN_ID" --stage <name> --status <DONE|FAILED> 2>/dev/null || true`. Phase→stage names: Phase 3 `investigate`, Phase 4 `build`, Phase 5 `coverage`, Phase 6 `review`, Phase 7 `e2e`, Phase 9 `merge` (`build`/`coverage`/`review`/`merge` map onto the dashboard's pipeline columns; the rest appear in the run's stage history).
> - **Close — on EVERY exit path once the run is open, not just Phase 11.** A run left open lingers `IN_PROGRESS` forever in the dashboard (the orchestrator is still alive, so dead-pid detection never fires). So before *any* terminal exit after `run-open`, close it:
>   - **Success** (Phase 11): `sdlc run-close --run "$RUN_ID" --status DONE --completed 1 2>/dev/null || true`.
>   - **Deliberate stop** (a Phase 2 stop condition — issue closed / assigned elsewhere / `wontfix`; or any other "STOP and report" before a fix is produced): `sdlc run-close --run "$RUN_ID" --status ABORTED --completed 0 2>/dev/null || true`.
>   - **Failure** (the Phase 8 bugfix loop exhausts its retries / `FIX_STATUS: UNFIXED`, a `MERGE_STATUS: CONFLICT|FAILED`, or any other failure exit): `sdlc run-close --run "$RUN_ID" --status FAILED --completed 0 2>/dev/null || true`.
>   - Only a genuine process crash should fall through to dead-pid detection — every reachable stop in this skill closes the run explicitly.

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
  - `--sequential` — disable parallel mode and process issues one at a time (batch mode only). By default, batch mode (`all`, `next --limit=N`) uses **parallel worktree isolation** — issues with overlapping files are serialized; independent issues run concurrently (max 5 agents per stage). Use `--sequential` to force the old one-at-a-time behavior.

**Removed flags** (fully autonomous mode — no user interaction):
- `--confirm` is removed — always proceed autonomously after investigation
- `--auto` is removed — autonomous behavior is now the default

Extract the issue number from the argument:
- If URL: extract number from path
- If `next`: run `gh issue list --label bug --state open --limit 1 --json number,title -q '.[0]'` to get the highest priority open bug
- If `all` or `opened issues`: run `gh issue list --state open --json number,title,labels --limit 50` to get all open issues, then process using parallel worktree mode (bugs first, then enhancements by priority). Use `--sequential` to force one-at-a-time processing.
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

Open a dashboard run (best-effort — see the run-logging note above) and capture `RUN_ID` for the rest of this issue:

```bash
RUN_ID=$(sdlc run-open --scope "issue-$ISSUE_NUMBER" --pid "$PPID" 2>/dev/null || true)
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

Before stopping on any of these, close the open dashboard run as aborted (the run-logging note above) so it does not linger `IN_PROGRESS`:

```bash
sdlc run-close --run "$RUN_ID" --status ABORTED --completed 0 2>/dev/null || true
```

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

## Phase 5: Coverage Gate (DISPATCHED — qa-engineer, sonnet) — skip if `--skip-coverage`

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Coverage check" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.45 --label "Phase 5: Coverage"'
```

Launch a `qa-engineer` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/coverage-gate-prompt.md`, substituting:
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

## Phase 7: E2E Gate (DISPATCHED — qa-engineer, sonnet) — skip unless `--e2e-gate=block|warn`

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "E2E testing" --icon hammer --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.73 --label "Phase 7: E2E"'
```

Launch a `qa-engineer` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/e2e-gate-prompt.md`, substituting:
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
- If `FIX_STATUS: UNFIXED` (or the 2-iteration cap is hit without a passing fix):
  - Log the failure details
  - Close the dashboard run as failed (the run-logging note above): `sdlc run-close --run "$RUN_ID" --status FAILED --completed 0 2>/dev/null || true`
  - Skip this issue (continue to next issue in batch mode, or STOP if single issue)

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

If `MERGE_STATUS: CONFLICT` or `MERGE_STATUS: FAILED` — log error, close the dashboard run as failed (`sdlc run-close --run "$RUN_ID" --status FAILED --completed 0 2>/dev/null || true`), and STOP.

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

## Phase 10b: Documentation Update (DISPATCHED — general-purpose, sonnet) — batch mode only

In batch mode (when multiple issues were fixed), update documentation once after all issues are processed:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status fix-issue "Updating docs" --icon pencil --color "#5856D6"'
bash -c '~/.claude/hooks/cmux-bridge.sh progress 0.95 --label "Phase 10b: Docs"'
```

If at least one issue was fixed successfully, launch a `general-purpose` agent (model: **sonnet**) with the prompt from `${CLAUDE_SKILL_DIR}/doc-update-prompt.md`, substituting:
- `{{SCOPE}}` → batch scope description (e.g., "all open issues" or "next 5 bugs")
- `{{COMPLETED_ISSUES}}` → comma-separated list of fixed issue numbers and titles
- `{{COMPLETED_PRS}}` → comma-separated list of merged PR numbers

The agent reviews all fixes and updates README + story docs in a single pass. **Non-blocking** — if it fails, the batch still reports success.

On success:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Documentation updated for [N] fixed issues" --source fix-issue'
```

On failure:
```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log warning "Documentation update failed — manual review needed" --source fix-issue'
```

Skip this phase entirely for single-issue fixes (the PostToolUse hook handles those).

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

Finalize the dashboard run (best-effort — see the run-logging note above):

```bash
sdlc run-close --run "$RUN_ID" --status DONE --completed 1 2>/dev/null || true
```

If the fix instead ended in a terminal failure/abort (e.g. the Phase 8 bugfix loop exhausted its retries), close it `FAILED` rather than `DONE`: `sdlc run-close --run "$RUN_ID" --status FAILED --completed 0 2>/dev/null || true`.

- Use `✅` if all gates passed cleanly, `⚠️` if any gate had warnings

### Batch Loop — Sequential (if `next --limit=N` or `all`, WITH `--sequential`)

If the original target was `next --limit=N` or `all`/`opened issues` and `--sequential` is set:
1. After completing (or skipping) the current issue, move to the next issue in the queue
2. Before each new issue: `git checkout main && git pull` to ensure clean state
3. If more issues remain: loop back to Phase 2 with the next issue
4. If no more issues: stop and print a batch summary of all issues processed (fixed / skipped / failed)

---

### Batch Loop — Parallel Worktree Mode (DEFAULT for `next --limit=N` or `all`)

In batch mode (default, unless `--sequential` is set), issues are processed using **batch-per-stage parallelism with git worktree isolation and a file-overlap guard**.

#### Phase P1: Fetch & Investigate All Issues in Parallel

1. **Fetch all issues** (DIRECT): Run `gh issue list` to get the full batch, then fetch metadata for each issue (Phase 2 logic) sequentially — this is fast and low-cost.

2. **Investigate all issues in parallel**: Launch up to 5 investigation agents concurrently (no worktree needed — read-only):

```
Agent(subagent_type="general-purpose", model="sonnet", prompt="""
[investigation-agent-prompt.md with substitutions for issue A]
""")
// + same for issues B, C, D, E — all in a single message
```

Collect `ROOT_CAUSE`, `FILES_TO_MODIFY`, `INVESTIGATION_STATUS` from each. Remove any `BLOCKED` issues from the batch.

#### Phase P2: File-Overlap Guard (DIRECT)

Compare `FILES_TO_MODIFY` across all investigated issues to detect potential merge conflicts:

```
Issue #42: src/auth/login.ts, src/auth/session.ts
Issue #57: src/api/routes.ts, src/utils/logger.ts
Issue #63: src/auth/login.ts, src/auth/middleware.ts  ← overlaps with #42!
Issue #71: tests/e2e/checkout.spec.ts
```

**Grouping algorithm:**
1. Build an undirected graph: issues are nodes, edges connect issues that share at least one file
2. Connected components become **serial groups** — issues within a group must be processed sequentially
3. Independent groups (no shared files) can run in parallel

From the example above:
- **Parallel Group A**: Issue #42, then Issue #63 (serialized — shared `src/auth/login.ts`)
- **Parallel Group B**: Issue #57 (independent)
- **Parallel Group C**: Issue #71 (independent)

Groups A, B, and C run their stages concurrently. Within Group A, #42 completes fully before #63 starts.

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Parallel batch: [N] issues in [G] groups ([S] serialized due to file overlap)" --source fix-issue'
```

#### Phase P3: Parallel Build (concurrent, worktree-isolated)

For all issues that can run concurrently (one per group), launch build agents with `isolation: "worktree"`:

```
Agent(
  subagent_type=[AGENT_TYPE],
  model="opus",
  isolation="worktree",
  prompt="""
  [build-agent-prompt.md with substitutions]
  IMPORTANT: Push the branch before returning:
    git push -u origin fix/issue-{{ISSUE_NUMBER}}-[slug]
  Return BRANCH_NAME and BUILD_STATUS.
  """
)
```

**Key rule:** In parallel mode, build agents ALWAYS push the branch (even when coverage gate is enabled), so coverage agents in separate worktrees can access it.

#### Phase P4: Parallel Coverage (concurrent, worktree-isolated)

Skip if `--skip-coverage`. For each successfully built issue, launch coverage agents concurrently with `isolation: "worktree"`:

```
Agent(
  subagent_type="qa-engineer",
  model="sonnet",
  isolation="worktree",
  prompt="""
  [coverage-gate-prompt.md with substitutions]
  NOTE: Branch {{BRANCH_NAME}} has been pushed. Start by:
    git fetch origin
    git checkout {{BRANCH_NAME}}
  """
)
```

#### Phase P5: Parallel Review (concurrent, no worktree)

Launch review agents concurrently (they use `gh` CLI, no worktree needed):

```
Agent(subagent_type="senior-code-reviewer", model="opus", prompt="""
[review-gate-prompt.md with substitutions for each issue]
""")
```

#### Phase P6: Parallel E2E (concurrent, worktree-isolated) — if enabled

Launch E2E agents concurrently with `isolation: "worktree"` (each needs to checkout the PR branch to run Playwright).

#### Phase P7: Sequential Merge (with rebase-before-merge)

For each approved issue **one at a time**:

Launch the merge agent with `${CLAUDE_SKILL_DIR}/merge-agent-prompt.md`. The merge agent includes a **rebase-before-merge step** to handle baseline drift from earlier merges.

After each merge:
- Log success/failure
- If this completes a serial group (e.g., #42 done in Group A), the next issue in that group (#63) can now start from Phase P3 in the next round
- Increment counters for batch summary

#### Phase P8: Serial Group Continuation

If any serial groups have remaining issues after the first round:
1. Loop back to Phase P3 for the next issue in each serial group
2. Independent issues that completed in round 1 are done
3. Continue until all serial groups are exhausted

#### Error Handling in Parallel Mode

- Investigation failures (Phase P1): Remove issue from batch, continue
- Build failures (Phase P3): Mark FAILED, exclude from subsequent stages. Route to bugfix agent (one at a time) if the failure is code/test related. Allow max 2 bugfix iterations.
- Coverage/Review/E2E failures: Same as sequential mode — bugfix loop applies per issue
- Merge conflicts (Phase P7): Rebase + retry, then bugfix if needed
- First failure triggers Telegram alert (same gate as sequential)

#### Progress Tracking in Parallel Mode

The orchestrator tracks progress centrally — agents do NOT update any shared progress files. Progress notifications update per-stage:

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh progress [FRACTION] --label "Stage [S]: [stage-name] ([N] issues)"'
```

## Context Budget Rules

**NEVER read project source files directly** — the build and investigation agents read them.
Your per-phase context cost should be minimal — delegate all heavy lifting to sub-agents.

## Context

$ARGUMENTS
