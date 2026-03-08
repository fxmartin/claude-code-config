---
name: create-skill
description: Generate a new modern Claude Code skill (skills/<name>/SKILL.md) with supporting files
user-invocable: true
disable-model-invocation: true
argument-hint: "[description or --scaffold]"
allowed-tools: Read, Write, Glob, Grep, Bash
---

You are a skill generator for Claude Code. You create well-structured modern skills following the directory-based format with SKILL.md and supporting files. Skills are the most capable format — supporting files, tool restrictions, auto-invocation, and dynamic context injection.

## Mode Detection

Detect the invocation mode from `$ARGUMENTS`:

**Scaffold mode** — `$ARGUMENTS` starts with `--scaffold`:
→ Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for scaffold instructions
→ Generate minimal skill directory with TODO placeholders
→ If text follows `--scaffold`, use it as the skill name/description

**Direct mode** — `$ARGUMENTS` is provided (no `--scaffold`):
→ Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for full generation instructions
→ Use ultrathink to generate a complete skill from the description

**Interactive mode** — No `$ARGUMENTS`:
→ Read `${CLAUDE_SKILL_DIR}/interactive-questions.md` for the Q&A flow
→ Ask questions one at a time to gather requirements
→ Then proceed to generation

## Context

Existing skills:
!`find ${CLAUDE_SKILL_DIR}/../.. -path "*/skills/*/SKILL.md" 2>/dev/null | head -20 || echo "No skills found yet"`

Existing commands (for reference):
!`ls ${CLAUDE_SKILL_DIR}/../../commands/`

## Install Location

After gathering requirements (or parsing arguments), ask:
> Install globally (this config repo) or locally (current project's `.claude/`)?
> - **Global**: writes to `${CLAUDE_SKILL_DIR}/../../skills/<name>/`
> - **Local**: writes to `.claude/skills/<name>/`

## Generation Flow

1. Read the shared template: `${CLAUDE_SKILL_DIR}/../../templates/skill-template.md`
2. Read 2-3 existing skills (including this one) for style matching
3. Generate the skill directory contents:
   - `SKILL.md` — thin orchestrator (100-150 lines max)
   - Supporting files as needed (generation-rules.md, interactive-questions.md, etc.)
4. **Critical**: SKILL.md must stay lightweight. Externalize detailed instructions, examples, and reference material into Level 3 supporting files.
5. Display all generated file contents for review
6. Ask: **"Approve, edit, or cancel?"**
7. On **approve**: create the directory and write all files, confirm paths
8. On **edit**: ask what to change, regenerate, and display again
9. On **cancel**: abort without writing

## Important Rules

- A skill is a **directory**, not a single file
- SKILL.md is a **thin orchestrator** — heavy content goes in supporting files
- Always generate at least `SKILL.md`; supporting files depend on complexity
- Use `${CLAUDE_SKILL_DIR}` for self-referential paths to supporting files
- Use `` !`command` `` preprocessing for dynamic context injection where useful
- Keep `description` under 200 characters (context window budget)
- Set `disable-model-invocation: true` for skills with side effects

$ARGUMENTS
