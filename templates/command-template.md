# Command Template Reference

This is a reference document for generating legacy Claude Code commands (`commands/<category>/<name>.md`).

## File Location

```
commands/<category>/<name>.md
```

Categories: `dev`, `quality`, `issues`, `project`, `devops`, `research`, or a new custom category.

## Optional Frontmatter

Commands can optionally include YAML frontmatter:

```yaml
---
allowed-tools: Read, Grep, Glob, Bash    # Restrict available tools
model: claude-sonnet-4-5-20250514        # Override default model
---
```

Most commands omit frontmatter entirely — add it only when tool restrictions or model overrides are needed.

## Body Structure

The body is a plain markdown prompt. Common patterns:

### Persona-Driven (most common)

```markdown
You are a [role] with [experience]. You [key traits].

Your approach:
- [trait 1]
- [trait 2]

[Detailed instructions for the task...]

$ARGUMENTS
```

### Structured Task

```markdown
You are tasked with [goal].

## Steps

1. [Step 1]
2. [Step 2]
3. [Step 3]

## Output Format

[Expected output structure]

$ARGUMENTS
```

### Interactive Q&A

```markdown
You are a [role]. Ask me one question at a time so we can develop [deliverable].

Each question should build on my previous answers.

Once done, [final action — e.g., create a file, generate a report].

$ARGUMENTS
```

## Variables

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | User input passed after the command name |

## Style Guidelines

- Lead with the persona/role definition
- Be specific about expertise level and domain
- Use direct, action-oriented language
- Define the output format explicitly
- End with `$ARGUMENTS` to receive user input
- Keep the prompt focused — one clear purpose per command
- If interactive, specify "one question at a time"

## When to Upgrade to a Skill

Consider upgrading to a skill (`skills/<namespace>/<name>/SKILL.md`) when you need:
- Supporting files (instructions, examples, reference docs)
- Tool restrictions per invocation
- Auto-invocation (model-triggered)
- Complex multi-step flows with conditional logic
- Dynamic context injection via `!`command`` preprocessing
