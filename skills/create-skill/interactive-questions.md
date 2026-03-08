# Interactive Question Flow for Skill Creation

When no arguments are provided, guide the user through these questions **one at a time**. Wait for each answer before asking the next.

## Questions

### 1. Purpose
> What should this skill do? Describe its purpose and the problem it solves.

### 2. Name
> What should this skill be called? Use lowercase letters, numbers, and hyphens only (e.g., `run-tests`, `lint-fixer`).
> The skill will be invoked as `/<name>`.

### 3. Invocation Style
> How should this skill be triggered?
> - **User-only** (`disable-model-invocation: true`): user must type the command explicitly — use this for skills with side effects like file creation
> - **Auto-invocable**: Claude can trigger it automatically when it detects a matching situation
> - **Both**: user can invoke manually, Claude can also auto-trigger

### 4. Arguments
> Does this skill accept arguments?
> - **Yes**: what kind? (e.g., a description, a file path, a URL)
> - **No**: it will always run interactively or with fixed behavior
>
> If yes, should it support `--scaffold` mode for quick templating?

### 5. Supporting Files
> Does this skill need supporting files beyond SKILL.md?
> - **generation-rules.md**: if it generates output (files, reports, configs)
> - **interactive-questions.md**: if it has an interactive Q&A mode
> - **instructions.md**: if it has complex domain-specific logic
> - **examples.md**: if example outputs would improve quality
> - **Other**: describe what's needed
>
> Reminder: SKILL.md should stay under 150 lines. Heavy content must go in supporting files.

### 6. Tool Restrictions
> What tools does this skill need? Choose the minimal set:
> - `Read, Grep, Glob` — read-only analysis
> - `Read, Write, Glob, Grep, Bash` — file creation and modification
> - `Read, Edit, Write, Grep, Glob, Bash` — full code modification
> - `WebFetch, WebSearch, Read, Write` — research and web access
> - Or specify a custom set

### 7. Dynamic Context
> Should the skill inject dynamic context at invocation time? For example:
> - List existing files in a directory
> - Show git status
> - Display environment info
>
> This uses `` !`command` `` preprocessing in SKILL.md.

### 8. Confirmation
Summarize the gathered requirements and ask:
> Here's what I'll generate:
> - **Skill**: `/<name>`
> - **Purpose**: `<purpose-summary>`
> - **Invocation**: `<user-only|auto|both>`
> - **Arguments**: `<argument-description>`
> - **Files**: `SKILL.md` + `<supporting-files>`
> - **Tools**: `<tool-list>`
>
> Ready to generate? (Or adjust anything?)

## Complexity Check

If the described skill seems simple enough for a command (no supporting files, no tool restrictions, no auto-invocation), suggest:
> This seems simple enough to be a **command** instead of a skill. Commands are single-file and easier to maintain. Want to use `/create-command` instead, or continue with a skill?
