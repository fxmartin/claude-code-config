# Generator Skills

Three skills for scaffolding new Claude Code components from within Claude Code itself.

## Quick Start

```bash
# Install (adds skills/ symlink to ~/.claude/)
./install.sh

# Generate a command from a description
/create-command "a command that generates changelog entries from git history"

# Generate an agent interactively
/create-agent

# Scaffold a skill with TODOs
/create-skill --scaffold "lint fixer"
```

## Available Skills

| Skill | Generates | Output |
|-------|-----------|--------|
| `/create-command` | Legacy slash commands | `commands/<category>/<name>.md` |
| `/create-agent` | Agent definitions | `agents/<name>.md` |
| `/create-skill` | Modern skills | `skills/<name>/SKILL.md` + directory |

## Three Invocation Modes

Every generator supports the same three modes:

### Interactive (no arguments)
```bash
/create-agent
```
Asks questions one at a time to gather requirements, then generates.

### Direct (with description)
```bash
/create-agent "a database query optimizer that analyzes slow queries and suggests indexes"
```
Generates a complete definition from the freeform description.

### Scaffold (quick template)
```bash
/create-agent --scaffold
/create-agent --scaffold "query optimizer"
```
Generates a minimal file with TODO placeholders. Optionally pre-fills name/description.

## Install Location

Each generator asks where to write the output:

- **Global** — writes to this config repo (shared across all projects via symlink)
- **Local** — writes to the current project's `.claude/` directory

## When to Use Which

| Need | Use |
|------|-----|
| Simple prompt, one file, no tool restrictions | `/create-command` |
| Auto-delegated sub-agent with specific tools | `/create-agent` |
| Multi-file skill with supporting docs, tool restrictions, or auto-invocation | `/create-skill` |

If you start with `/create-command` and the requirements grow complex, the skill will suggest upgrading to `/create-skill`.

## Templates

Shared reference templates in `templates/` define the canonical structure for each component type:

- `templates/skill-template.md` — SKILL.md frontmatter fields, body patterns, supporting file conventions
- `templates/agent-template.md` — agent frontmatter, description format with `<example>` blocks, body structure
- `templates/command-template.md` — command patterns (persona-driven, structured task, interactive Q&A)

Templates are read by the generators at generation time — not loaded into context upfront.

## Architecture

Each skill follows a **thin orchestrator** pattern to minimize token usage:

```
skills/create-<type>/
├── SKILL.md                 # ~100 lines: mode detection, flow control, file refs
├── generation-rules.md      # Detailed generation instructions (loaded on demand)
└── interactive-questions.md  # Q&A flow (loaded only in interactive mode)
```

**Loading levels:**
1. **Level 1** — Frontmatter (always): name + description (~100 tokens)
2. **Level 2** — SKILL.md body (on invocation): thin orchestrator
3. **Level 3** — Supporting files (on demand): only when Claude reads them
