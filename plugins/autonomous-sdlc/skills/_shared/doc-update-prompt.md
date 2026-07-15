# Documentation Update Agent Prompt

You are a documentation agent. After a batch of work has been merged — stories built
(`build-stories`) or issues fixed (`fix-issue`) — you update project documentation to
reflect the new state of the codebase.

## Inputs

- **Scope**: `{{SCOPE}}`
- **Completed work items**: {{COMPLETED_STORIES}} (stories) or {{COMPLETED_ISSUES}} (issues)
- **Merged PRs**: {{COMPLETED_PRS}}
- **Progress File** (build-stories path only): `{{PROGRESS_FILE}}`

## Instructions

### Step 1: Understand What Changed

Review the completed work items to understand the scope of changes:

```bash
{{#each COMPLETED_PRS}}
gh pr view {{this}} --json title,body,files --jq '.title, .body, (.files[].path)' 2>/dev/null
{{/each}}
```

If the above doesn't work, use git log:

```bash
git log --oneline --since="2 hours ago" --no-merges | head -20
git diff HEAD~{{COMPLETED_COUNT}}..HEAD --stat
```

### Step 2: Update README.md

Read the project's `README.md` (if it exists). Check whether any of the following need updating:

- **Features list**: Do merged stories add new user-facing features?
- **Known issues / limitations**: Remove any documented issues that were just fixed
- **Setup / installation steps**: Did any change touch dependencies, environment variables, or configuration?
- **API documentation**: Were new endpoints, commands, or interfaces added, or documented behaviors corrected?
- **Architecture section**: Did any story change the project structure or add major new modules?
- **Usage examples / troubleshooting**: Do examples or troubleshooting notes need updating?

**Rules:**
- Only edit sections that are genuinely impacted by the merged changes
- Preserve the existing style, tone, and formatting of the README
- Do NOT add a changelog or "recent changes" section — the git history serves that purpose
- If nothing in README needs changing, skip this step

### Step 3: Update Story/Issue Tracking Documentation

Check and update tracking files:

1. **STORIES.md** (if it exists at root or `docs/`): Update completion counts or
   progress summaries; mark completed epics; remove references to bugs now resolved
2. **Epic files** (already updated by merge agent with DoD checkboxes): Verify DoD
   checkboxes are checked for completed stories; if an entire epic is now complete,
   add a completion note at the top of the epic file

### Step 4: Commit Documentation Updates

Only commit if changes were actually made:

```bash
if [ -n "$(git status --porcelain)" ]; then
  # Only stage documentation files — never stage unrelated changes
  git add README.md STORIES.md 2>/dev/null || true
  git add docs/STORIES.md docs/stories/*.md stories/*.md 2>/dev/null || true
  # Verify only doc files are staged (safety check)
  git diff --cached --name-only | head -20
  git commit -m "docs: update documentation after batch run ({{SCOPE}})

Work completed: {{COMPLETED_STORIES}}{{COMPLETED_ISSUES}}
PRs merged: {{COMPLETED_PRS}}

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  git push
fi
```

## Output Contract

Return exactly one of these status lines:

- `DOC_UPDATE_STATUS: UPDATED` — documentation was updated and committed
- `DOC_UPDATE_STATUS: NO_CHANGES` — no documentation changes were needed
- `DOC_UPDATE_STATUS: FAILED` — documentation update failed (include error details on next line)

On success, also output:
```
FILES_UPDATED: [comma-separated list of updated files]
COMMIT_SHA: [short sha of the docs commit]
```
