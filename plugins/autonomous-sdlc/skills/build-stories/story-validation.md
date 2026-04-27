# Story Validation Reference

This document defines the validation rules applied to stories during discovery. Validation is **warn-only** — flagged stories are still included in the build queue.

## Required Fields

| Field | Detection Pattern | Default if Missing |
|-------|------------------|--------------------|
| **Story ID** | `### Story X.Y-NNN` or `#### X.Y-NNN` heading pattern | Skip story (cannot build without ID) |
| **Title** | Text after the Story ID on the same heading line | `"Untitled Story"` |
| **Priority** | `Priority:` or `**Priority**:` followed by value | `Should Have` |
| **Story Points** | `Points:` or `**Points**:` followed by number | `3` |
| **Acceptance Criteria** | Section heading containing `Acceptance Criteria` (any heading level) | Flag as warning |
| **Definition of Done** | Checkbox list (`- [ ]` or `- [x]`) under a `DoD` or `Definition of Done` heading | Flag as warning |

## Minimum Content Check

A story section is considered **too thin** if it meets any of these criteria:

- **Fewer than 5 lines** of content (excluding blank lines and the heading itself)
- **No acceptance criteria section** — stories without AC are difficult to verify
- **No DoD checkboxes** — stories without DoD cannot be marked complete by automation

## Validation Output Format

Validation warnings are collected and emitted as a `### Validation Warnings` section in the discovery agent output. Each warning includes:

```
### Validation Warnings

| Story ID | Warning | Detail |
|----------|---------|--------|
| 01.1-001 | Missing AC | No "Acceptance Criteria" section found |
| 02.1-003 | Thin story | Only 3 lines of content (minimum: 5) |
| 03.2-001 | No DoD | No Definition of Done checkboxes found |
```

## Validation Rules Summary

1. **ID format**: Must match pattern `NN.N-NNN` (epic.feature-story)
2. **Acceptance criteria**: At least one bullet or numbered item under an AC heading
3. **DoD block**: At least one checkbox item under a DoD heading
4. **Priority value**: Must be one of `Must Have`, `Should Have`, `Could Have`, `Won't Have`
5. **Content depth**: Minimum 5 non-blank lines of body content
6. **Story points**: Must be a positive integer

Validation does NOT block the build queue — it produces warnings that appear in the discovery output and the final summary report.
