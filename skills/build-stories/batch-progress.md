# Batch Progress Tracking

## Progress File Location

Store at: `docs/stories/.build-progress.md` (relative to project root)

If `docs/stories/` doesn't exist, try `stories/` instead.

## Progress File Format

```markdown
# Build Progress

**Started**: 2026-03-12T10:30:00Z
**Last Updated**: 2026-03-12T14:22:00Z
**Scope**: all | epic-01 | resume
**Mode**: auto | interactive

## Queue

| Order | Story ID | Title | Status | PR | Branch | Started | Completed |
|-------|----------|-------|--------|-----|--------|---------|-----------|
| 1 | 01.1-001 | Project Setup | DONE | #12 | feature/01.1-001 | 10:30 | 10:45 |
| 2 | 01.1-002 | Database Schema | DONE | #13 | feature/01.1-002 | 10:46 | 11:15 |
| 3 | 01.2-001 | Auth API | IN_PROGRESS | #14 | feature/01.2-001 | 11:16 | - |
| 4 | 01.2-002 | Auth Middleware | PENDING | - | - | - | - |
| 5 | 02.1-001 | Dashboard Layout | SKIPPED | - | - | - | - |
| 6 | 01.3-001 | User Profile | FAILED | #15 | feature/01.3-001 | 12:00 | 12:30 |

## Summary

- **Total**: 6
- **Done**: 2
- **In Progress**: 1
- **Failed**: 1
- **Skipped**: 1
- **Pending**: 1
```

## Status Values

| Status | Meaning | On Resume |
|--------|---------|-----------|
| `DONE` | Story built, reviewed, merged | Skip |
| `IN_PROGRESS` | Was being built when interrupted | Restart from scratch (clean branch) |
| `PENDING` | Not yet started | Build normally |
| `FAILED` | Build or review failed | In `--auto` mode: skip. Otherwise: prompt user |
| `SKIPPED` | Manually skipped or blocked | Re-evaluate dependencies; build if unblocked |
| `BLOCKED` | Dependency not met | Re-evaluate; may become PENDING |

## E2E Gate Rows

E2E gates appear as special rows in the progress queue:

```markdown
| Order | Story ID | Title | Status | PR | Branch | Started | Completed |
|-------|----------|-------|--------|-----|--------|---------|-----------|
| 3.e2e | epic-01 | E2E Gate: Epic 01 | E2E_PASS | - | - | 11:20 | 11:35 |
```

### E2E Status Values

| Status | Meaning | On Resume |
|--------|---------|-----------|
| `E2E_PASS` | All tests written and passing | Skip |
| `E2E_FAIL` | Tests still failing after max retries | Re-run gate |
| `E2E_WARN` | Tests failed, user chose to continue | Skip |
| `E2E_SKIP` | Gate was off | Skip |

### E2E Resume Cleanup

When resuming an `E2E_FAIL` gate:
1. Check if test file `e2e/epic-[ID].spec.ts` exists
2. If yes, run existing tests first (skip generation)
3. If tests pass now (e.g., code was fixed manually), mark `E2E_PASS`
4. If still failing, enter fix loop

## Resume Logic

When `$ARGUMENTS` contains `resume`:

1. Read `docs/stories/.build-progress.md`
2. If file doesn't exist → error: "No previous build session found. Use `all` or `epic-NN` to start."
3. For each story in the queue:
   - `DONE` → skip
   - `IN_PROGRESS` → clean up any existing branch, restart
   - `FAILED` → if `--auto`: mark SKIPPED. Otherwise: ask user to retry or skip
   - `PENDING` → proceed normally
   - `SKIPPED` / `BLOCKED` → re-evaluate dependencies
4. Continue execution from first non-DONE story

## Progress Updates

Update the progress file at these points:
- **Before starting a story**: Set status to `IN_PROGRESS`, record start time, branch name
- **After successful merge**: Set status to `DONE`, record PR number, completion time
- **On failure**: Set status to `FAILED`, record PR number if created
- **On skip**: Set status to `SKIPPED`
- **After each update**: Recalculate summary counts

## Cleanup on IN_PROGRESS Resume

When resuming a story that was `IN_PROGRESS`:

1. Check if branch exists: `git branch --list "feature/$STORY_ID"`
2. If exists locally: delete it `git branch -D feature/$STORY_ID`
3. Check if PR exists: `gh pr list --head "feature/$STORY_ID"`
4. If open PR exists: close it `gh pr close --delete-branch`
5. Ensure on main: `git checkout main && git pull`
6. Start fresh

## Batch Summary

At the end of a batch run, output:

```markdown
## Batch Build Summary

**Duration**: 2h 15m
**Stories Processed**: 6

| Status | Count |
|--------|-------|
| DONE | 4 |
| FAILED | 1 |
| SKIPPED | 1 |

### Completed
- 01.1-001: Project Setup (#12)
- 01.1-002: Database Schema (#13)
- 01.2-001: Auth API (#14)
- 01.2-002: Auth Middleware (#16)

### Failed
- 01.3-001: User Profile — Build agent encountered type errors

### Skipped
- 02.1-001: Dashboard Layout — Blocked by 01.3-001
```
