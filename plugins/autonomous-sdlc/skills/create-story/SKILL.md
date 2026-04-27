---
name: create-story
description: Interactively add one or more new stories to an existing epic. Reads STORIES.md and the target epic, asks clarifying questions, then writes stories matching the repo's established format.
user-invocable: true
disable-model-invocation: true
argument-hint: "<epic-number> [story description]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

> **cmux environment check** — this skill emits cmux sidebar updates via `cmux-bridge.sh`. Before emitting any call whose subcommand is `status`, `progress`, `log`, or `clear`, check whether the `$CMUX_SOCKET_PATH` environment variable is set. If it is **empty** (running outside cmux — e.g. Claude Desktop App), **skip every such call in this skill**: they only drive the cmux sidebar UI and produce no effect elsewhere. Always run `cmux-bridge.sh notify` and `cmux-bridge.sh telegram` calls regardless of environment — they deliver to Telegram even when cmux is absent.

This is the companion to `create-epic` for adding stories to an existing epic. Use it when the user wants to extend an epic that already exists rather than create a new one.

## Context

Check for existing stories structure:
!`ls docs/stories/epic-*.md docs/STORIES.md 2>/dev/null || echo "No existing story files"`
!`ls STORIES.md 2>/dev/null || echo "No root STORIES.md"`

## Invocation

Examples:

- `/create-story 12 add crash recovery for mission queue`
- `/create-story EPIC-18 skill health check dashboard`
- `/create-story add OAuth expiry alerts`

Parse `$ARGUMENTS` as:

- **Epic number** (required): numeric identifier (`03`, `12`, or normalized from `EPIC-12`)
- **Story description** (required): brief description of the story or stories to add

If either parameter is missing, ask for the missing value before editing files. If both are missing, ask for both in one concise message.

## Discovery

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status create-story "Discovery" --icon sparkle --color "#007AFF"'
bash -c '~/.claude/hooks/cmux-bridge.sh log info "Story discovery started" --source create-story'
```

Before asking detailed product questions or writing files:

1. Read `STORIES.md` or the repo's equivalent story index.
2. List all existing epic files in the story directory.
3. Read the requested epic file.
4. Check neighboring or similarly named epics to confirm the new story belongs in the requested epic.
5. If the requested epic looks wrong, explain the likely better target and ask for confirmation before proceeding.

Use repository evidence over the user's shorthand. Completed epics can still receive follow-up stories, but explicitly confirm that the user intends to reopen or extend a completed epic.

## Clarifying Questions

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status create-story "Interview" --icon sparkle --color "#007AFF"'
```

Ask questions **one at a time**, building on previous answers. Ask enough questions to make the story implementation-ready, but do not over-interview. Skip any question already answered by the epic, existing stories, or the user's description.

Cover:

1. User or operator goal
2. Business value or operational outcome
3. Scope boundaries and explicit non-goals
4. Expected behavior and important edge cases
5. Data, API, UI, security, or runtime constraints
6. Dependencies on other epics, shipped systems, or external services
7. Acceptance criteria expectations
8. Priority, MVP status, story points, and risk

If the description naturally splits into multiple independently deliverable outcomes, say so and propose the story split. Ask for confirmation unless the user already requested multiple stories.

## Generation

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status create-story "Generating story" --icon sparkle --color "#FF9500"'
bash -c '~/.claude/hooks/cmux-bridge.sh log progress "Discovery complete — generating story" --source create-story'
```

Update the requested epic file. Match the repository's existing conventions exactly:

- Epic filename pattern, usually `docs/stories/epic-{NN}-{kebab-name}.md`
- Feature table structure
- Story ID scheme, usually `{epic}.{feature}-NNN`
- Story heading format
- User story wording
- Priority, story points, dependencies, and risk fields
- Given/When/Then acceptance criteria
- Technical notes and Definition of Done checklist style

When adding stories:

1. Place each story under the most relevant existing feature when possible.
2. Create a new feature section only when the story does not fit an existing feature.
3. Choose the next story number without renumbering existing stories.
4. Keep stories INVEST-friendly and independently implementable.
5. Add acceptance criteria that are testable and specific.
6. Include security, privacy, or operational constraints when relevant to the epic.
7. Update epic totals, feature tables, story counts, point totals, MVP counts, status, or summary text only when the new story changes them.
8. Update `STORIES.md` or the repo's equivalent index when story count, point total, status, priority, or summary changes.

Prefer one well-scoped story over a bundle. Split into multiple stories when one request spans distinct user workflows, risky implementation layers, or separate verification surfaces.

## Output

After writing files, report:

- Target epic and why it was confirmed as the right one
- Story IDs and titles created
- Files updated
- Story count and point total changes
- Assumptions made
- Verification performed, or why verification was not applicable

```bash
bash -c '~/.claude/hooks/cmux-bridge.sh status create-story "Complete" --icon sparkle --color "#34C759"'
bash -c '~/.claude/hooks/cmux-bridge.sh log success "Story added to epic" --source create-story'
bash -c '~/.claude/hooks/cmux-bridge.sh notify "Story Created" "Added to epic"'
bash -c '~/.claude/hooks/cmux-bridge.sh telegram "✅ Story Created" "Added to epic"'
```

## Guardrails

- Do not add implementation code; this skill only updates story planning artifacts.
- Do not invent major architecture decisions while writing stories. Capture uncertain decisions as open questions or technical notes.
- Do not renumber existing stories.
- Do not edit production data or deployment configuration.
- Keep unrelated roadmap cleanup out of the diff; mention drift separately if found.

$ARGUMENTS
