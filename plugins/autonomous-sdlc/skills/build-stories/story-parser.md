# Story Parser Rules

## Epic File Discovery

1. Read `STORIES.md` (or `docs/STORIES.md`) for the epic index
2. Glob for epic files: `docs/stories/epic-*.md` or `stories/epic-*.md`
3. Also check for NFR file: `**/non-functional-requirements.md`

## Story ID Patterns

Story IDs follow the format `[Epic].[Feature]-NNN`:
- `01.1-001` — Epic 01, Feature 1, Story 001
- `02.3-002` — Epic 02, Feature 3, Story 002

Section headers use: `##### Story [ID]: [Title]`

Regex for story header:
```
^#{4,5}\s+Story\s+(\d+\.\d+-\d+):\s+(.+)$
```

## Completion Detection

A story is **complete** when ALL Definition of Done checkboxes are checked:
```
- [x] Code implemented and peer reviewed
- [x] Tests written and passing
- [x] Documentation updated
```

A story is **incomplete** if ANY DoD checkbox is unchecked:
```
- [ ] Code implemented and peer reviewed
```

### Detection Algorithm

1. For each story section, find the `**Definition of Done**:` block
2. Count `- [ ]` (unchecked) and `- [x]` (checked) items
3. Story is DONE only if unchecked count == 0 and checked count > 0
4. Story with no DoD block is treated as INCOMPLETE (needs attention)

## Priority Extraction

Priority field format: `**Priority**: Must Have | Should Have | Could Have`

Priority order (highest first):
1. `Must Have` (weight: 3)
2. `Should Have` (weight: 2)
3. `Could Have` (weight: 1)

## Dependency Extraction

Dependencies field format: `**Dependencies**: [story IDs or "None"]`

Parse comma-separated story IDs:
```
**Dependencies**: 01.1-001, 01.2-003
**Dependencies**: None
```

## Story Points Extraction

Format: `**Story Points**: [N]`

## Agent Type Detection

Detect from story content — look for keywords in title, acceptance criteria, and technical notes:

| Keywords | Agent Type |
|----------|-----------|
| API, endpoint, backend, server, middleware, database, migration | backend (detect TS vs Python from project) |
| UI, frontend, component, page, form, dashboard, responsive | ui-engineer |
| container, docker, podman, deploy, image, orchestrat | podman-container-architect |
| script, automat, CI/CD, pipeline, workflow, shell, bash | bash-zsh-macos-engineer |
| test, coverage, quality, QA, validation | qa-engineer |
| refactor, review, architecture, security audit | senior-code-reviewer |

For backend language detection, check project files:
- `*.ts` / `package.json` / `bun.lockb` → `backend-typescript-architect`
- `*.py` / `pyproject.toml` / `uv.lock` → `python-backend-engineer`

## Output Format

Each parsed story produces a record:

```
{
  id: "01.2-001",
  title: "Story Title",
  epic_id: "01",
  epic_name: "Epic Name",
  priority: "Must Have",
  priority_weight: 3,
  story_points: 3,
  dependencies: ["01.1-001", "01.1-002"],
  is_complete: false,
  dod_checked: 1,
  dod_total: 3,
  agent_type: "python-backend-engineer",
  epic_file: "docs/stories/epic-01-name.md"
}
```
