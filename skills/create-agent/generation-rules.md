# Agent Generation Rules

## Full Generation Mode

When generating a complete agent from a description, use ultrathink to produce high-quality output.

### Step 1: Determine Metadata

- **Name**: derive a concise kebab-case name (e.g., `dependency-manager`, `api-tester`)
- **Color**: choose from `Red, Blue, Green, Yellow, Purple, Orange, Pink, Cyan` — avoid colors already used by existing agents when possible
- **Tools**: infer the minimal set from the agent's purpose:

| Agent Type | Typical Tools |
|------------|---------------|
| Read-only analysis | `Read, Grep, Glob` |
| Code review | `Read, Grep, Glob, Bash` |
| Code modification | `Read, Edit, Write, Grep, Glob, Bash` |
| Research / web | `WebFetch, WebSearch, Read, Write` |
| File generation | `Write, Read, Glob` |
| Full access | `All tools` |

### Step 2: Write the Description (Critical)

The `description` field must be on a **single line** and include:

1. **Trigger statement**: "Use this agent when [specific condition]"
2. **At least one `<example>` block** showing the delegation flow:

```
Use this agent when [condition]. Examples: <example>Context: [situation]. user: '[user message]' assistant: '[delegation response]' <commentary>[why this agent is appropriate]</commentary></example>
```

**Good descriptions** are specific and action-oriented:
- "Use this agent when you need to analyze database query performance and suggest optimizations"
- "Use proactively when the user asks to set up CI/CD pipelines"

**Bad descriptions** are vague:
- "Helps with databases" (too vague, won't trigger)
- "A general helper" (no delegation signal)

### Step 3: Write the Body

Follow this exact structure:

```markdown
# Purpose

You are a [specific role with expertise level and years of experience]. [One-sentence mission].

## Instructions

When invoked, follow these steps:

1. **[Action verb]**: [First step — typically analyze/understand the context]
2. **[Action verb]**: [Core action — the main work]
3. **[Action verb]**: [Validation/verification step]
4. [Additional steps as needed — keep to 5-10 steps max]

## Best Practices

- [Domain-specific best practice 1]
- [Domain-specific best practice 2]
- [Common pitfall to avoid]
- [Quality standard to maintain]

## Report / Response

[Define the output format explicitly:]
- Start with [summary/overview]
- Organize by [severity/category/priority]
- Include [specific details expected]
- End with [recommendations/next steps]
```

### Step 4: Quality Checklist

- [ ] Name is kebab-case and descriptive
- [ ] Description includes trigger condition and `<example>` block
- [ ] Tool set is minimal — no unnecessary tools granted
- [ ] Color doesn't conflict with existing agents
- [ ] Body follows `# Purpose` → `## Instructions` → `## Best Practices` → `## Report / Response`
- [ ] Instructions are numbered and use action verbs
- [ ] Role definition is specific (not "an expert" but "a Senior DevOps Engineer with 10+ years")
- [ ] Output format is explicitly defined

## Scaffold Mode

When `--scaffold` is specified, generate a minimal template:

```markdown
---
name: <name>
description: Use this agent when [TODO: trigger condition]. Examples: <example>Context: [TODO]. user: '[TODO]' assistant: '[TODO]' <commentary>[TODO]</commentary></example>
tools: Read, Grep, Glob
color: Green
---

# Purpose

You are a [TODO: define role and expertise].

## Instructions

When invoked, follow these steps:

1. **Analyze**: [TODO: understand context]
2. **Execute**: [TODO: core action]
3. **Validate**: [TODO: verify results]

## Best Practices

- [TODO: domain-specific best practices]

## Report / Response

[TODO: define output format]
```

If text follows `--scaffold`, use it to pre-fill the name, description trigger, and role with reasonable defaults.
