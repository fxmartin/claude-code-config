# Skill Template Reference

This is a reference document for generating modern Claude Code skills (`skills/<namespace>/<name>/SKILL.md`).

## File Location

```
skills/<namespace>/<name>/SKILL.md
```

A skill is a **directory** containing `SKILL.md` plus optional supporting files.

## Frontmatter (Required)

```yaml
---
name: <display-name>
description: <concise-description-under-200-chars>
user-invocable: true
disable-model-invocation: true
argument-hint: "[description]"
allowed-tools: Read, Write, Glob, Grep, Bash
---
```

### All Available Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | Display name shown in skill list |
| `description` | string | — | Brief description (keep under 200 chars for context budget) |
| `user-invocable` | bool | `true` | Whether users can invoke via `/<namespace>:<name>` |
| `disable-model-invocation` | bool | `false` | If `true`, only user can invoke (not auto-triggered) |
| `argument-hint` | string | — | Shown in help text (e.g., `"[url]"`, `"<file-path>"`) |
| `allowed-tools` | string | all | Comma-separated list of allowed tools |
| `model` | string | default | Override model (e.g., `claude-sonnet-4-5-20250514`) |
| `context` | string | — | `"fork"` to run in forked context (no interactive Q&A) |
| `agent` | object | — | Agent configuration for delegation |
| `hooks` | object | — | Pre/post execution hooks |

### Key Decisions

- **`disable-model-invocation: true`** — Use for skills with side effects (file creation, git operations)
- **`context: fork`** — Use for fire-and-forget tasks; incompatible with interactive Q&A
- **`allowed-tools`** — Always specify minimal set; omit for unrestricted access

## Body Structure (Thin Orchestrator Pattern)

The SKILL.md body should be a **thin orchestrator** (~100-150 lines max). Heavy content goes in supporting files.

```markdown
You are [role]. [One-sentence purpose].

## Mode Detection

If `$ARGUMENTS` contains `--scaffold`:
  → Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for scaffold mode instructions
  → Generate minimal structure with TODO placeholders

If `$ARGUMENTS` is provided (without --scaffold):
  → Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for full generation instructions
  → Generate complete output from the description

If no `$ARGUMENTS`:
  → Read `${CLAUDE_SKILL_DIR}/interactive-questions.md` for Q&A flow
  → Ask questions one at a time to gather requirements
  → Then read generation-rules.md and generate

## Context

Existing items:
!`ls ${CLAUDE_SKILL_DIR}/../../../relevant-directory/`

## Generation

1. Read the shared template: `${CLAUDE_SKILL_DIR}/../../../templates/<type>-template.md`
2. Read 2-3 existing examples for style matching
3. Generate content following template structure
4. Display for review
5. Ask: "Approve, edit, or cancel?"
6. On approval: write to chosen location
```

## Supporting Files

| File | Purpose | When Loaded |
|------|---------|-------------|
| `generation-rules.md` | Detailed generation instructions, output format, best practices | On generation step |
| `interactive-questions.md` | Q&A flow for interactive mode | Only when no `$ARGUMENTS` |
| `instructions.md` | Domain-specific detailed instructions | On demand |
| `examples.md` | Example outputs or patterns | On demand |
| `reference.md` | Reference material | On demand |

## Variables

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | User input after the skill name |
| `${CLAUDE_SKILL_DIR}` | Absolute path to the skill's directory (resolves through symlinks) |

## Dynamic Context Injection

Use `` !`command` `` preprocessing to inject live context:

```markdown
!`ls ${CLAUDE_SKILL_DIR}/../../../agents/`
```

This runs at invocation time, giving Claude awareness of existing files without reading them.

## Token Optimization

| Loading Level | What | When |
|---------------|------|------|
| Level 1 — Metadata | Frontmatter | Always (skill discovery) |
| Level 2 — Instructions | SKILL.md body | On invocation |
| Level 3 — Resources | Supporting files | On demand (Claude reads them) |

Keep SKILL.md body under 150 lines. Externalize:
- Detailed step-by-step instructions → `generation-rules.md`
- Interactive Q&A flows → `interactive-questions.md`
- Domain knowledge → topic-specific `.md` files
