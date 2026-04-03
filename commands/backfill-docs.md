You are performing a **one-off documentation backfill**. Many GitHub issues were closed and merged without updating project documentation. Your job is to review all closed issues, understand what changed, and produce a single comprehensive documentation update.

Parse `$ARGUMENTS` for the target repository in `owner/repo` format (e.g., `fxmartin/my-project`). If no argument is provided, auto-detect from the current git remote:

```bash
git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]\(.*\)\.git|\1|' | sed 's|.*github.com[:/]\(.*\)|\1|'
```

If detection fails, STOP and ask the user to provide the repo.

## Phase 1: Inventory Closed Issues

Fetch all closed issues with their linked PRs:

```bash
gh issue list --repo $REPO --state closed --json number,title,labels,closedAt,body --limit 100 --jq '.[] | "\(.number)\t\(.title)\t\(.labels | map(.name) | join(","))\t\(.closedAt)"'
```

Store the output. Count the total. Print: `Found [N] closed issues in [REPO].`

Then for each closed issue, fetch the linked/merged PR:

```bash
gh pr list --repo $REPO --state merged --search "closes #<NUMBER> OR fixes #<NUMBER>" --json number,title,headRefName,mergedAt,files --limit 1
```

If that returns nothing for some issues, also try:

```bash
gh issue view <NUMBER> --repo $REPO --json comments --jq '.comments[].body' | grep -oP '#\d+' | head -1
```

Build and print a structured inventory table:

```
| Issue # | Title | Labels | Closed At | Linked PR # | Files Changed |
```

## Phase 2: Categorize Changes

Group the closed issues by impact type. Read the PR diffs to understand what actually changed:

```bash
gh pr view <PR_NUMBER> --repo $REPO --json files --jq '.files[].path'
gh pr diff <PR_NUMBER> --repo $REPO --stat
```

Categorize each issue into one or more of:

1. **New Feature** — adds user-facing functionality (should be in README features section)
2. **Bug Fix** — corrects broken behavior (remove from known issues if documented, update troubleshooting)
3. **API Change** — modifies endpoints, CLI flags, configuration (update API docs, usage examples)
4. **Dependency Change** — adds/removes/updates dependencies (update setup/installation steps)
5. **Infrastructure** — CI/CD, tooling, internal refactoring (generally no doc impact)
6. **Breaking Change** — changes behavior in incompatible ways (critical to document)

Print the categorization summary before proceeding.

## Phase 3: Analyze Current Documentation

Read all existing documentation files to understand what's currently documented:

```bash
find . -name "README.md" -o -name "STORIES.md" -o -name "CHANGELOG.md" -o -name "*.md" -path "*/docs/*" | head -30
```

For each documentation file found, read it and note:
- What features/capabilities are currently documented
- What known issues or limitations are listed
- What setup/installation steps exist
- What API documentation exists
- What is outdated or missing based on your Phase 2 categorization

## Phase 4: Produce Documentation Updates

Now make the actual edits. Follow these rules strictly:

### README.md Updates

- **Features section**: Add any new features from Category 1 that aren't already listed. Match the existing style and tone.
- **Known issues / Limitations**: Remove any items that were fixed (Category 2). If a "Known Issues" section becomes empty, remove the section header too.
- **Setup / Installation**: Update if any Category 4 (dependency) or Category 6 (breaking) changes affect it.
- **API / Usage / Configuration**: Update if any Category 3 changes modified the interface. Ensure examples are still accurate.
- **Troubleshooting**: Add notes for significant bug fixes that users might encounter on older versions.

### STORIES.md / Epic Files

- Update completion status for any stories that were implemented via the closed issues
- Check off DoD items if the issue corresponds to a story's acceptance criteria
- Add completion notes for fully completed epics

### General Rules

- **Do NOT create a CHANGELOG.md** — git history and GitHub issues serve that purpose
- **Do NOT add "last updated" dates** — they go stale immediately
- **Preserve existing formatting** — match the style, tone, heading levels, and structure
- **Be surgical** — only change what the closed issues actually impact
- **Group related changes** — if 5 issues all improved the auth module, write one cohesive update, not 5 separate bullet points
- **Skip infrastructure issues** (Category 5) unless they changed something user-facing

## Phase 5: Verify Changes

Before committing, review what you've changed:

```bash
git diff --stat
git diff
```

Sanity check:
- Are all edits factually correct based on the PR diffs you reviewed?
- Did you accidentally remove content that's still relevant?
- Are the changes proportional to what actually shipped? (Don't over-document minor fixes)
- Is the README still coherent when read top to bottom?

## Phase 6: Commit and Push

Only commit documentation files:

```bash
git add README.md STORIES.md 2>/dev/null || true
git add docs/*.md 2>/dev/null || true
git add docs/stories/*.md stories/*.md 2>/dev/null || true

# Verify staging — only doc files
git diff --cached --name-only

git commit -m "docs: backfill documentation from [N] closed issues

Reviewed [N] closed issues and [M] merged PRs.
Updated: [list of files changed]
Categories: [N] features, [N] bug fixes, [N] API changes, [N] dependency updates

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"

git push
```

## Phase 7: Print Summary

Print a structured summary:

```markdown
## Documentation Backfill Complete

**Repository**: [REPO]
**Issues Reviewed**: [total]
**PRs Analyzed**: [total]

### Changes Made

| File | Changes | Issues Covered |
|------|---------|---------------|
| README.md | [summary] | #1, #4, #12 |
| docs/... | [summary] | #7, #15 |

### Categories Breakdown
- Features documented: [N]
- Bug fixes reflected: [N]
- API changes updated: [N]
- No doc impact (infra): [N]

### Skipped Issues
[List any issues skipped and why]
```

## Context Budget

With 20-100 issues, process in batches of ~20 to stay within context. Batch by category (features first, then bug fixes, etc.) and make multiple smaller commits if needed. Each commit should be self-contained and the README should be coherent after every commit.

$ARGUMENTS
