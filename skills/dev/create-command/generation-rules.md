# Command Generation Rules

## Full Generation Mode

When generating a complete command from a description:

### Step 1: Determine Metadata
- **Name**: derive a concise kebab-case filename (e.g., `generate-changelog.md`)
- **Category**: choose from existing categories (`dev`, `quality`, `issues`, `project`, `devops`, `research`) or propose a new one
- **Frontmatter**: only add if tool restrictions or model override are needed; most commands have no frontmatter

### Step 2: Write the Command Body

Follow one of these patterns based on the command's nature:

**Persona-Driven** (default for most commands):
```markdown
You are a [specific role] with [experience level]. [Key traits and approach].

Your style:
- [Trait 1]
- [Trait 2]
- [Trait 3]

[Detailed task instructions...]

$ARGUMENTS
```

**Structured Task** (for procedural commands):
```markdown
You are tasked with [goal].

## Steps

1. [Step 1]
2. [Step 2]
3. [Step 3]

## Output Format

[Expected structure]

$ARGUMENTS
```

**Interactive Q&A** (for discovery-based commands):
```markdown
You are a [role]. Ask me one question at a time so we can develop [deliverable].

Each question should build on my previous answers.

Once done, [final action].

$ARGUMENTS
```

### Step 3: Quality Checklist
- [ ] Persona is specific and authoritative (not generic)
- [ ] Instructions are actionable and unambiguous
- [ ] Output format is defined (if applicable)
- [ ] `$ARGUMENTS` is present at the end
- [ ] Tone matches existing commands in the category
- [ ] No unnecessary frontmatter
- [ ] Single clear purpose — not trying to do too much

## Scaffold Mode

When `--scaffold` is specified, generate a minimal template:

```markdown
<!-- TODO: Add optional frontmatter if needed -->
<!-- ---
allowed-tools: Read, Grep, Glob, Bash
--- -->

You are a [TODO: define role and expertise].

[TODO: describe the task and approach]

## Steps

1. [TODO: first step]
2. [TODO: next steps]

## Output Format

[TODO: define expected output]

$ARGUMENTS
```

If text follows `--scaffold`, use it to pre-fill the role and task description with reasonable defaults, but keep TODO markers for sections that need human refinement.

## Best Practices

- Study 2-3 existing commands in the target category before generating
- Match the voice and detail level of sibling commands
- Prefer imperative instructions over passive descriptions
- Be specific about expertise: "Senior DevOps engineer with 10+ years" beats "an expert"
- Define what the command should NOT do (scope boundaries) when relevant
- For interactive commands, always specify "one question at a time"
- Keep the total prompt under ~100 lines for readability
