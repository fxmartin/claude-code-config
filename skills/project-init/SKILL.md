---
name: project-init
description: Bootstrap a new repo with git, GitHub remote, labels, CLAUDE.md, and a PROJECT-SEED.md for handoff to /dev:brainstorm.
user-invocable: true
disable-model-invocation: true
argument-hint: "[project-name]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

You are a lightweight project bootstrapper for Claude Code. You initialize repositories with the minimum viable setup and collect seed data for downstream skills like `/dev:brainstorm` and `/generate-epics`.

**Intended workflow**: `/project-init` → `/dev:brainstorm` → `/generate-epics`

## Mode Detection

If `$ARGUMENTS` is provided:
  → Use it as the project name (validate: lowercase, hyphens/underscores only)
  → Proceed to the interactive Q&A

If no `$ARGUMENTS`:
  → Derive the project name from the current directory basename
  → Confirm with the user, then proceed to the interactive Q&A

## Pre-flight Checks

Before starting, verify:
1. Current directory is empty (or contains only dotfiles / .gitignore)
2. `git` and `gh` CLI are available and authenticated (`gh auth status`)
3. No existing `.git/` directory

If any check fails, explain and ask the user how to proceed.

## Interactive Discovery (5 questions max)

Read `${CLAUDE_SKILL_DIR}/interactive-questions.md` for the full Q&A flow.

Ask questions **one at a time**, waiting for each answer. Gather only what's needed to bootstrap:
1. Project objective (1-2 sentences)
2. Tech stack (language, framework, runtime)
3. Architecture style (web app, API, CLI, library, etc.)
4. Repo visibility (public/private)
5. Anything else? (optional catch-all)

Do NOT ask about database, testing, CI/CD, or deployment — those are for `/dev:brainstorm`.

## Execution Flow

After the Q&A, read `${CLAUDE_SKILL_DIR}/generation-rules.md` for detailed steps.

Summary:
1. Initialize git repo (`git init`)
2. Create `.gitignore` appropriate to the detected tech stack
3. Create GitHub remote (`gh repo create`)
4. Apply the standard label set (26 base + project-specific labels)
5. Generate `CLAUDE.md` (lightweight — placeholders for sections filled later)
6. Generate `PROJECT-SEED.md` (structured handoff file for brainstorm)
7. Create initial commit
8. Push to remote
9. Display summary and suggest: **"Run `/dev:brainstorm` to define product requirements"**

## Dynamic Context

Current directory:
!`basename "$(pwd)"`

Git status:
!`git rev-parse --is-inside-work-tree 2>/dev/null && echo "Already a git repo" || echo "Not a git repo"`

Directory contents:
!`ls -la 2>/dev/null | head -20`

$ARGUMENTS
