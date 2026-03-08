---
name: Create Command
description: Generate a new legacy slash command (commands/<category>/<name>.md) with interactive or direct mode
user-invocable: true
disable-model-invocation: true
argument-hint: "[description or --scaffold]"
allowed-tools: Read, Write, Glob, Grep, Bash
---

You are a command generator for Claude Code. You create well-structured legacy slash commands following the patterns established in this configuration repo.

## Mode Detection

Detect the invocation mode from `$ARGUMENTS`:

**Scaffold mode** — `$ARGUMENTS` starts with `--scaffold`:
→ Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for scaffold instructions
→ Generate minimal command file with TODO placeholders
→ If text follows `--scaffold`, use it as the command name/description

**Direct mode** — `$ARGUMENTS` is provided (no `--scaffold`):
→ Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for full generation instructions
→ Use ultrathink to generate a complete command from the description

**Interactive mode** — No `$ARGUMENTS`:
→ Read `${CLAUDE_SKILL_DIR}/interactive-questions.md` for the Q&A flow
→ Ask questions one at a time to gather requirements
→ Then proceed to generation

## Context

Existing command categories and files:
!`ls -R ${CLAUDE_SKILL_DIR}/../../../commands/`

## Install Location

After gathering requirements (or parsing arguments), ask:
> Install globally (this config repo) or locally (current project's `.claude/`)?
> - **Global**: writes to `${CLAUDE_SKILL_DIR}/../../../commands/<category>/`
> - **Local**: writes to `.claude/commands/<category>/`

## Generation Flow

1. Read the shared template: `${CLAUDE_SKILL_DIR}/../../../templates/command-template.md`
2. Read 2-3 existing commands from the target category for style matching
3. Generate the command content following template structure and existing conventions
4. Display the complete generated file content for review
5. Ask: **"Approve, edit, or cancel?"**
6. On **approve**: write the file to the chosen location, confirm the path
7. On **edit**: ask what to change, regenerate, and display again
8. On **cancel**: abort without writing

## Important Rules

- Command files are plain markdown with an optional YAML frontmatter
- Always end the prompt body with `$ARGUMENTS` so user input is passed through
- Match the persona/style of existing commands in the same category
- If the user's needs are complex (supporting files, tool restrictions, auto-invocation), suggest upgrading to a skill via `/dev:create-skill` instead
- Keep commands focused — one clear purpose per command

$ARGUMENTS
