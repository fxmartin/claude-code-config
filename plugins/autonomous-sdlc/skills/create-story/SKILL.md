---
name: create-story
description: Interactively add one or more new stories to an epic. If no epic number is given, infers the best-fit epic from the requirement and STORIES.md. If the requirement is too large for a single story, recommends running /create-epic instead.
user-invocable: true
disable-model-invocation: true
argument-hint: "[epic-number] <story description>"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

> **Notifications** — this skill sends Telegram pings at lifecycle milestones via `~/.claude/hooks/notify-telegram.sh "<title>" "<body>"`, called unconditionally (Telegram-only; a silent no-op when unconfigured). There are no sidebar or desktop notifications.

This is the companion to `create-epic` for adding stories to an existing epic. Use it when the user wants to extend an epic that already exists rather than create a new one. The skill can infer the right epic from the requirement alone, and will redirect the user to `create-epic` when the requirement is too large to fit one story.

## Context

Check for existing stories structure:
!`ls docs/stories/epic-*.md docs/STORIES.md 2>/dev/null || echo "No existing story files"`
!`ls STORIES.md 2>/dev/null || echo "No root STORIES.md"`

## Invocation

Examples:

- `/create-story 12 add crash recovery for mission queue` — explicit epic
- `/create-story EPIC-18 skill health check dashboard` — explicit epic, normalized form
- `/create-story add OAuth expiry alerts` — epic inferred from requirement
- `/create-story` — ask for the requirement, then infer the epic

Parse `$ARGUMENTS` as:

- **Epic number** (optional): numeric identifier (`03`, `12`, or normalized from `EPIC-12`). If the first whitespace-separated token matches `^(EPIC-?)?\d+$`, treat it as the epic number; otherwise the entire `$ARGUMENTS` is the story description.
- **Story description** (required): brief description of the story or stories to add.

If the story description is missing, ask for it in one concise message before doing anything else. If the epic number is missing, do **not** ask — proceed to Discovery and infer it.

## Discovery

Before asking detailed product questions or writing files:

1. Read `STORIES.md` or the repo's equivalent story index.
2. List all existing epic files in the story directory.
3. **Resolve the target epic**:
   - If the user passed an epic number, read that epic file. Then check neighboring or similarly named epics to confirm the story belongs there. If the requested epic looks wrong, explain the likely better target and ask for confirmation before proceeding.
   - If no epic number was passed, **infer it**. Score each open epic by overlap with the requirement: matching domain keywords, named systems/components, personas, and acceptance-criteria themes already present in that epic's stories. Read the top 1–3 candidates' files to confirm fit. Present the chosen epic to the user with a one-line "why this one" justification and one runner-up, and ask for confirmation before continuing. If no epic is a clear fit, say so and recommend `/create-epic` (see Scope Check below).
4. Use repository evidence over the user's shorthand. Completed epics can still receive follow-up stories, but explicitly confirm that the user intends to reopen or extend a completed epic.

## Scope Check

Before opening clarifying questions, evaluate whether the requirement actually fits inside one (or a small handful of) story. Recommend `/create-epic` instead when **any** of these hold:

- The requirement spans **multiple distinct user workflows** or personas that don't share acceptance criteria.
- A first-pass decomposition produces **more than ~4 implementation-ready stories**, or any single story would exceed **8 story points**.
- It introduces a **new architectural surface** (new service, new datastore, new external integration) not already owned by any existing epic.
- It implies **cross-cutting concerns** (security model change, new compliance regime, platform-wide migration) that warrant their own success metrics.
- **No existing epic** is a defensible home and creating a one-story orphan epic would be worse than a proper epic with a roadmap.

When triggered:

1. State plainly: "This looks bigger than one story — recommend running `/create-epic` instead."
2. Give 1–2 sentences on why (which trigger fired).
3. Sketch the rough epic shape: proposed name, 3–6 candidate stories, suggested epic number.
4. Ask: "Proceed with `/create-epic`, or force-fit this as a single stretched story anyway?"
5. Only continue as a story if the user explicitly chooses to force-fit. Capture the deferred scope as open questions in the story's Technical Notes.

## Clarifying Questions

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

- Target epic and why it was confirmed as the right one (note whether it was given or inferred)
- Story IDs and titles created
- Files updated
- Story count and point total changes
- Assumptions made
- Verification performed, or why verification was not applicable

If the Scope Check fired and you redirected the user to `/create-epic`, **do not** emit the Telegram "Story Created" notification below — the skill produced no story.

```bash
bash -c '~/.claude/hooks/notify-telegram.sh "✅ Story Created" "Added to epic"'
```

## Guardrails

- Do not add implementation code; this skill only updates story planning artifacts.
- Do not invent major architecture decisions while writing stories. Capture uncertain decisions as open questions or technical notes.
- Do not renumber existing stories.
- Do not edit production data or deployment configuration.
- Keep unrelated roadmap cleanup out of the diff; mention drift separately if found.

$ARGUMENTS
