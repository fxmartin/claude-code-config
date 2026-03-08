# Skill Generation Rules

## Full Generation Mode

When generating a complete skill from a description, use ultrathink to produce high-quality output.

### Step 1: Determine Metadata

- **Name**: lowercase kebab-case (e.g., `create-skill`, `run-tests`) — this becomes the `/slash-command`
- **Directory name**: same as name (e.g., `create-skill/SKILL.md`)
- **Invocation**: `/<name>` (e.g., `/create-skill`)

### Step 2: Choose Frontmatter Fields

Required fields:
```yaml
name: <display-name>
description: <under-200-chars>
user-invocable: true
```

Decide on optional fields based on the skill's nature:

| Field | When to Set |
|-------|-------------|
| `disable-model-invocation: true` | Skill creates files, sends messages, or has other side effects |
| `argument-hint` | Skill accepts arguments (show hint in help) |
| `allowed-tools` | Restrict tools to minimal needed set |
| `model` | Need a specific model (e.g., faster model for simple tasks) |
| `context: fork` | Fire-and-forget task (incompatible with interactive Q&A) |

### Step 3: Design the SKILL.md Body (Thin Orchestrator)

The SKILL.md body must stay under **150 lines**. It should:

1. **Define the role** in 1-2 sentences
2. **Detect mode** from `$ARGUMENTS` (scaffold, direct, interactive)
3. **Reference supporting files** via `${CLAUDE_SKILL_DIR}/filename.md`
4. **Inject dynamic context** via `` !`command` `` where useful
5. **Define the generation/execution flow** as numbered steps
6. **Include review cycle** (display → approve/edit/cancel)

**Anti-patterns to avoid:**
- Embedding detailed instructions directly in SKILL.md
- Listing all possible options/fields inline
- Including example outputs in the orchestrator
- Duplicating content from templates or supporting files

### Step 4: Design Supporting Files

Determine which supporting files are needed:

| File | Include When |
|------|-------------|
| `generation-rules.md` | Skill generates output (files, reports, configs) |
| `interactive-questions.md` | Skill supports interactive mode (no-argument invocation) |
| `instructions.md` | Complex domain-specific logic that doesn't fit elsewhere |
| `examples.md` | Multiple example outputs are helpful for quality |
| `reference.md` | Domain reference material (APIs, specs, standards) |

**Minimum**: most skills need at least `generation-rules.md`.
**Interactive skills**: also need `interactive-questions.md`.
**Simple skills**: may only need `SKILL.md` with no supporting files.

### Step 5: Write Supporting Files

**generation-rules.md** should contain:
- Detailed step-by-step generation instructions
- Output format specification
- Quality checklist
- Scaffold mode template
- Best practices for the domain

**interactive-questions.md** should contain:
- Numbered questions (one at a time)
- Suggested answers or options for each
- A final confirmation/summary step
- Complexity check (suggest upgrading if needed)

### Step 6: Quality Checklist

- [ ] SKILL.md is under 150 lines
- [ ] Description is under 200 characters
- [ ] Frontmatter includes all needed fields
- [ ] `$ARGUMENTS` is referenced for mode detection
- [ ] Supporting files are referenced via `${CLAUDE_SKILL_DIR}/`
- [ ] Dynamic context injection used where appropriate
- [ ] Review cycle included (approve/edit/cancel)
- [ ] Detailed instructions externalized to supporting files
- [ ] `disable-model-invocation: true` set if skill has side effects

## Scaffold Mode

When `--scaffold` is specified, generate minimal files:

**SKILL.md:**
```markdown
---
name: <kebab-case-name>
description: <TODO: brief description under 200 chars>
user-invocable: true
disable-model-invocation: true
argument-hint: "[TODO: argument hint]"
allowed-tools: Read, Write, Glob, Grep, Bash
---

You are a [TODO: define role]. [TODO: one-sentence purpose].

## Mode Detection

If `$ARGUMENTS` contains `--scaffold`:
  → Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for scaffold mode
  → [TODO: scaffold behavior]

If `$ARGUMENTS` is provided:
  → Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for full generation
  → [TODO: direct mode behavior]

If no `$ARGUMENTS`:
  → Read `${CLAUDE_SKILL_DIR}/interactive-questions.md` for Q&A flow
  → [TODO: interactive behavior]

## Generation Flow

1. [TODO: define steps]
2. Display for review
3. Ask: "Approve, edit, or cancel?"
4. On approval: write files

$ARGUMENTS
```

**generation-rules.md:**
```markdown
# Generation Rules

## Full Generation Mode

[TODO: detailed generation instructions]

## Scaffold Mode

[TODO: scaffold template]

## Best Practices

[TODO: domain-specific best practices]
```

If text follows `--scaffold`, use it to pre-fill the name, description, and role.

## Best Practices

- Study this skill (`create-skill`) as a reference implementation
- The thin orchestrator pattern is mandatory — never embed heavy content in SKILL.md
- Use `${CLAUDE_SKILL_DIR}` for all supporting file references (resolves through symlinks)
- Use `` !`command` `` for dynamic context that changes between invocations
- Supporting files are only loaded when Claude explicitly reads them (Level 3)
- Keep frontmatter descriptions action-oriented and concise
- Test that the generated skill's directory path is valid before writing
