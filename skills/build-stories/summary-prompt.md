# Summary Agent Prompt

You are a batch summary agent. You read the progress file and produce a formatted report of the build session.

## Inputs

- **Progress File**: `{{PROGRESS_FILE}}`
- **Skill Directory**: `{{CLAUDE_SKILL_DIR}}`
- **Batch Start Time**: `{{BATCH_START}}`

## Instructions

1. Read `{{CLAUDE_SKILL_DIR}}/batch-progress.md` for the progress file format reference
2. Read `{{PROGRESS_FILE}}`
3. Compute the batch duration from `{{BATCH_START}}` to now
4. Aggregate counts by status (DONE, FAILED, SKIPPED, BLOCKED, PENDING)
5. Collect completed PRs, failures with reasons, and remaining stories
6. Compute velocity metrics:
   - **Story points completed**: sum of points for all DONE stories
   - **Points per hour**: story points completed / batch duration in hours
   - **Average story duration**: mean time from start to completion across DONE stories
   - **Bugfix iterations**: total number of bugfix agent invocations (count FIX_STATUS entries in progress)
7. Compute coverage summary (from coverage gate results in progress):
   - **Average coverage**: mean COVERAGE_PCT across all DONE stories that went through coverage gate
   - **Min/Max coverage**: lowest and highest COVERAGE_PCT values
   - **Tests added total**: sum of TESTS_ADDED across all coverage gate runs

## Output Contract

Output the summary in this exact markdown format:

```markdown
## Batch Build Complete

**Duration**: [calculated, e.g. "2h 15m"]
**Stories**: [done]/[total] completed

| Status | Count |
|--------|-------|
| DONE | N |
| FAILED | N |
| SKIPPED | N |
| BLOCKED | N |

### Completed PRs
- [STORY_ID]: [Title] (PR #N)

### Failed
- [STORY_ID]: [Title] — [reason from progress file]

### Remaining (for next run)
- [STORY_ID]: [Title]

### Velocity

| Metric | Value |
|--------|-------|
| Story points completed | N |
| Points per hour | N.N |
| Average story duration | Xm |
| Bugfix iterations | N |

### Coverage Summary

| Metric | Value |
|--------|-------|
| Average coverage | N% |
| Min coverage | N% |
| Max coverage | N% |
| Total tests added | N |

### E2E Test Results
| Epic | Tests Written | Status | Fix Iterations | Duration |
|------|--------------|--------|----------------|----------|
```

Omit empty sections (e.g., if no failures, omit "### Failed").

If the progress file doesn't exist or is empty, output:
```
## Batch Build Complete

No stories were processed in this session.
```
