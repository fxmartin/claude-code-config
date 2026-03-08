# Agent Template Reference

This is a reference document for generating Claude Code agent definitions (`agents/<name>.md`).

## File Location

```
agents/<name>.md
```

Name format: `kebab-case` (e.g., `dependency-manager`, `api-tester`).

## Frontmatter (Required)

```yaml
---
name: <kebab-case-name>
description: <delegation-description-with-examples>
tools: <Tool1>, <Tool2>, <Tool3>
color: <Color>
---
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Kebab-case identifier |
| `description` | Yes | Action-oriented delegation description. Critical for auto-delegation. |
| `tools` | Yes | Comma-separated list of allowed tools (minimal set) |
| `color` | No | Terminal color: Red, Blue, Green, Yellow, Purple, Orange, Pink, Cyan |

### Description Format (Critical)

The `description` field determines when Claude auto-delegates to this agent. It MUST include:
1. A concise statement of when to use the agent
2. At least one `<example>` block showing delegation context

```yaml
description: Use this agent when [trigger condition]. Examples: <example>Context: [situation]. user: '[user message]' assistant: '[delegation message]' <commentary>[reasoning]</commentary></example>
```

### Common Tool Sets

| Agent Type | Tools |
|------------|-------|
| Read-only analysis | Read, Grep, Glob |
| Code modification | Read, Edit, Write, Grep, Glob, Bash |
| Research/web | WebFetch, WebSearch, Read, Write |
| Full access | All tools |

## Body Structure

```markdown
# Purpose

You are a [role definition with expertise level and domain].

## Instructions

When invoked, follow these steps:

1. [First step — typically analyze/understand]
2. [Second step — core action]
3. [Third step — validate/verify]
4. [Continue as needed...]

## Best Practices

- [Domain-specific best practice]
- [Quality standard]
- [Common pitfall to avoid]

## Report / Response

[Define output format — structure, sections, level of detail]
```

## Style Guidelines

- Keep `description` on a single line (YAML frontmatter constraint)
- Include 1-2 `<example>` blocks in the description for reliable auto-delegation
- Choose the minimal tool set — don't grant tools the agent won't use
- Number instructions sequentially for clear execution order
- Define explicit output format so responses are consistent
- Use imperative voice in instructions ("Analyze", "Identify", "Generate")
- Include "Best Practices" section for domain-specific guidance
