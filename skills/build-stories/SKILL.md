---
name: build-stories
description: Batch build all incomplete stories across epics — parses stories, resolves dependencies, launches build/review agents sequentially, merges PRs, and tracks progress for resumability.
user-invocable: true
disable-model-invocation: true
argument-hint: "[all|resume|epic-NN|epic-name] [--dry-run] [--auto]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

You are a batch story orchestrator. You read all epics, identify incomplete stories, resolve dependencies, and build them sequentially — each with a full lifecycle of build, review, merge, and progress tracking.

## Argument Parsing

Parse `$ARGUMENTS` for:
- **Scope**: `all` | `resume` | `epic-NN` (e.g., `epic-03`) | `<epic-name>` (e.g., `authentication`)
  - Default to `all` if no scope provided
- **Flags**:
  - `--dry-run` — show ordered build queue without executing
  - `--auto` — skip failed stories automatically (no interactive prompts)
  - `--e2e-gate=block|warn|off` — E2E test gate behavior between epics (default: `block`)
  - `--skip-e2e` — shorthand for `--e2e-gate=off`

## Phase 1: Setup & Validation

1. Verify working directory contains `STORIES.md` or `docs/STORIES.md`
2. Verify clean git state: `git status --porcelain` must be empty (or stash)
3. Verify on main branch: `git branch --show-current`
4. Pull latest: `git pull`
5. Verify GitHub CLI: `gh auth status`

## Phase 2: Story Discovery

Read `${CLAUDE_SKILL_DIR}/story-parser.md` for parsing rules.

1. Read STORIES.md for epic index
2. Glob for epic files: `docs/stories/epic-*.md` or `stories/epic-*.md`
3. For each epic file, parse ALL stories extracting:
   - Story ID, title, priority, story points
   - Dependencies
   - DoD completion status (checked vs unchecked boxes)
   - Agent type (auto-detect from story content and project tech stack)
4. Filter by scope argument
5. Separate into: complete (skip) and incomplete (build candidates)

## Phase 3: Dependency Resolution

Read `${CLAUDE_SKILL_DIR}/dependency-resolver.md` for the full algorithm.

1. Build DAG from incomplete stories
2. Run topological sort with priority tiebreaking
3. Detect cycles — STOP if found
4. Identify blocked stories (cross-scope dependencies)
5. Produce ordered build queue

## Phase 4: Resume Check

Read `${CLAUDE_SKILL_DIR}/batch-progress.md` for progress file format and resume rules.

If scope is `resume`:
1. Load `docs/stories/.build-progress.md`
2. Apply resume logic (skip DONE, restart IN_PROGRESS, handle FAILED)
3. Re-evaluate SKIPPED/BLOCKED stories

If scope is NOT `resume` but progress file exists:
- Warn user: "Previous build session found. Use `resume` to continue or current scope to start fresh."
- If starting fresh, archive old progress file (rename with timestamp)

## Phase 5: Dry Run (if --dry-run)

Display the build queue as a formatted table and stop:

```
## Build Queue (dry run)

| # | Story ID | Title | Priority | Points | Agent | Dependencies |
|---|----------|-------|----------|--------|-------|-------------|

### Blocked Stories
[if any]

### Already Complete
[list]

Total: N stories, M story points
Estimated: [N stories to build]
```

If `--e2e-gate` is not `off`, insert `--- E2E Gate: Epic NN ---` separator rows in the build queue table at epic boundaries.

Then STOP — do not execute.

## Phase 6: Execute Build Loop

For each story in the ordered queue:

### Step 1: Update Progress
- Set story status to `IN_PROGRESS` in progress file
- Record start time and branch name

### Step 2: Launch Build Agent
Use the Agent tool to launch the appropriate specialized agent:

```
Agent(subagent_type="<detected-agent-type>", prompt="""
You are building story [STORY_ID]: [TITLE]

Epic: [EPIC_NAME] (from [EPIC_FILE])
Priority: [PRIORITY]

## User Story
[paste full user story text from epic file]

## Acceptance Criteria
[paste acceptance criteria]

## Technical Notes
[paste technical notes if any]

## Instructions
1. Create branch: git checkout -b feature/[STORY_ID]
2. Follow TDD: write failing tests first, then implement
3. Run all quality gates (see below)
4. Commit with message: feat([epic-name]): [story title] (#[STORY_ID])
5. Push and create PR:
   git push -u origin feature/[STORY_ID]
   gh pr create --title "feat: [Story Title] (#[STORY_ID])" --body "[PR body with summary, testing, closes #STORY_ID]"

## Quality Gates
- All tests passing
- Type checking passes
- Linting passes
- No security vulnerabilities introduced

Return the PR number and URL when done.
""")
```

### Step 3: Launch Review Agent
After the build agent completes, launch the review agent:

```
Agent(subagent_type="senior-code-reviewer", prompt="""
Review the PR created for story [STORY_ID]: [TITLE]

1. Run: gh pr view [PR_NUMBER] to get PR details
2. Run: gh pr diff [PR_NUMBER] to review changes
3. Check for:
   - Architecture consistency
   - Security vulnerabilities
   - Performance issues
   - Test coverage adequacy
   - Code quality and maintainability
4. If changes needed:
   - Check out the branch: git checkout feature/[STORY_ID]
   - Make fixes directly
   - Commit and push
   - Re-review
5. When satisfied, approve: gh pr review [PR_NUMBER] --approve

Return approval status.
""")
```

### Step 4: Merge & Cleanup
After review approval:

```bash
gh pr merge [PR_NUMBER] --squash --delete-branch
git checkout main
git pull
```

### Step 5: Update Epic DoD
Edit the epic file to check off DoD items for the completed story:
- Change `- [ ]` to `- [x]` for all DoD items of this story

### Step 6: Update Progress
- Set story status to `DONE`
- Record PR number and completion time
- Update summary counts

### Step 7: E2E Gate Check

Read `${CLAUDE_SKILL_DIR}/e2e-gate.md` for the full E2E gate logic.

After completing a story, check if this was the last story for its epic in the build queue. If so and `--e2e-gate` is not `off`:
1. Launch `qa-expert` agent to generate E2E tests from the epic's acceptance criteria
2. Agent explores the UI via Playwright MCP, writes tests, runs them
3. On failure: agent fixes code/tests and reruns (max 5 iterations)
4. Record E2E gate result in progress file
5. Handle failure per `--e2e-gate` mode (block/warn)

### Error Handling
If any step fails:
- Set story status to `FAILED` in progress file
- If `--auto` flag: log failure reason, continue to next story
- If no `--auto` flag: ask user whether to retry, skip, or abort the batch
- On abort: save progress file for future `resume`

## Phase 7: Batch Summary

After all stories processed (or on abort), display:

```
## Batch Build Complete

**Duration**: [calculated]
**Stories**: [done]/[total] completed

| Status | Count |
|--------|-------|
| DONE | N |
| FAILED | N |
| SKIPPED | N |
| BLOCKED | N |

### Completed PRs
- [STORY_ID]: [Title] (PR #N)
...

### Failed
- [STORY_ID]: [Title] — [reason]
...

### Remaining (for next run)
- [STORY_ID]: [Title]
...

### E2E Test Results
| Epic | Tests Written | Status | Fix Iterations | Duration |
|------|--------------|--------|----------------|----------|
```

## Context

Project structure:
!`ls -d */ 2>/dev/null | head -20`

Existing stories:
!`ls docs/stories/epic-*.md 2>/dev/null || ls stories/epic-*.md 2>/dev/null || echo "No epic files found"`

Previous build progress:
!`cat docs/stories/.build-progress.md 2>/dev/null || cat stories/.build-progress.md 2>/dev/null || echo "No previous build session"`

$ARGUMENTS
