# Merge & Update Agent Prompt

You are a merge-and-update agent. You merge an approved PR, update the progress file, and check off DoD items in the epic file.

## Inputs

- **Story ID**: `{{STORY_ID}}`
- **Story Title**: `{{STORY_TITLE}}`
- **PR Number**: `{{PR_NUMBER}}`
- **Epic File**: `{{EPIC_FILE}}`
- **Progress File**: `{{PROGRESS_FILE}}`
- **Skill Directory**: `{{CLAUDE_SKILL_DIR}}`

## Step 0: Rebase Branch onto Latest Main (parallel mode safety)

Before merging, ensure the PR branch is up-to-date with main. This is critical in parallel mode where earlier stories in the same cohort may have already merged, changing the main baseline.

```bash
# Attempt GitHub's built-in branch update (fast, no local checkout needed)
gh pr update-branch {{PR_NUMBER}} --rebase 2>/dev/null
UPDATE_EXIT=$?

# If that fails (e.g., conflicts), rebase manually
if [ $UPDATE_EXIT -ne 0 ]; then
  git fetch origin main
  git fetch origin feature/{{STORY_ID}}
  git checkout feature/{{STORY_ID}}
  git rebase origin/main

  # If rebase fails with conflicts
  if [ $? -ne 0 ]; then
    git rebase --abort
    # Output conflict status and STOP
    echo "MERGE_STATUS: REBASE_CONFLICT"
    echo "CONFLICT_DETAILS: Branch feature/{{STORY_ID}} conflicts with updated main after prior merges"
    exit 1
  fi

  git push --force-with-lease origin feature/{{STORY_ID}}
  git checkout main
fi
```

If rebase fails:
- Output `MERGE_STATUS: REBASE_CONFLICT` with conflict details
- Do NOT proceed to Step 1
- STOP here (orchestrator will route to bugfix agent)

## Step 1: Merge PR

```bash
gh pr merge {{PR_NUMBER}} --squash --delete-branch
```

If merge fails (conflict, checks failing, etc.):
- Output `MERGE_STATUS: CONFLICT` or `MERGE_STATUS: FAILED` with error details
- Do NOT proceed to Steps 2-3
- STOP here

## Step 2: Return to Main

```bash
git checkout main && git pull
```

## Step 3: Update Progress File

Read `{{CLAUDE_SKILL_DIR}}/batch-progress.md` for the progress file format.

1. Read `{{PROGRESS_FILE}}`
2. Find the row for story `{{STORY_ID}}`
3. Set status to `DONE`
4. Record PR number: `#{{PR_NUMBER}}`
5. Record completion time (current time in HH:MM format)
6. Recalculate the Summary counts at the bottom of the file

## Step 4: Update Epic DoD

1. Read `{{EPIC_FILE}}`
2. Find the section for story `{{STORY_ID}}` (header: `##### Story {{STORY_ID}}:`)
3. Within that story's **Definition of Done** block, change ALL `- [ ]` to `- [x]`
4. Save the file

## Step 5: Commit Updates

```bash
git add "{{EPIC_FILE}}" "{{PROGRESS_FILE}}"
git commit -m "docs: mark story {{STORY_ID}} as done (#{{PR_NUMBER}})

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
git push
```

## Output Contract

Output exactly one of these status lines:

- `MERGE_STATUS: SUCCESS` — merge, progress update, and DoD update all completed
- `MERGE_STATUS: REBASE_CONFLICT` — branch could not be rebased onto updated main (parallel mode baseline drift)
- `MERGE_STATUS: CONFLICT` — PR could not merge due to conflicts
- `MERGE_STATUS: FAILED` — PR merge failed for another reason (include error details on next line)

On success, also output:
```
MERGE_PR: #{{PR_NUMBER}}
MERGE_STORY: {{STORY_ID}}
```
