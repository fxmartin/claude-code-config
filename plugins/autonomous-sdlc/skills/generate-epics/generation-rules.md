# Story Generation Rules

## Requirements Analysis (Step 1)

Analyze REQUIREMENTS.md to understand:
- Product vision and business objectives
- User personas and target audience
- Functional and non-functional requirements
- Technical constraints and dependencies
- Success criteria and acceptance thresholds

## User Story Format

```
As a [user type/persona], I want [functionality] so that [business value/benefit]
```

## INVEST Criteria

Every story must be:
- **Independent**: Can be developed standalone
- **Negotiable**: Open to discussion and refinement
- **Valuable**: Delivers clear business value
- **Estimable**: Can be sized and estimated
- **Small**: Fits within a single sprint
- **Testable**: Has clear acceptance criteria

## STORIES.md Template

```markdown
# USER STORIES - PROJECT OVERVIEW

## User Personas
### Primary Personas
#### [Persona Name]
- **Role**: [description]
- **Goals**: [objectives]
- **Pain Points**: [challenges]

## Epic Overview
| Epic ID | Epic Name | Business Value | Story Count | Total Points | Priority |
|---------|-----------|----------------|-------------|--------------|----------|

## Epic Navigation
- **[Epic-01: Name](./stories/epic-01-[name].md)** - [Brief description]

## MVP Summary
### MVP Criteria / MVP Scope / MVP Epic Breakdown

## Project Metrics
- **Total Stories**: [N]
- **Total Story Points**: [N]

## Story Dependencies
### Cross-Epic Dependencies (mermaid diagram)
### Critical Path
```

## Epic File Template

```markdown
# Epic [N]: [Name]

## Epic Overview
**Epic ID**: Epic-[N]
**Description**: [comprehensive description]
**Business Value**: [benefit]
**Success Metrics**: [measurable outcomes]

## Epic Scope
**Total Stories**: [N] | **Total Points**: [N] | **MVP Stories**: [N]

## Features in This Epic

### Feature [Epic].[Feature]: [Name]

#### Stories

##### Story [Epic].[Feature]-001: [Title]
**User Story**: As a [persona], I want [functionality] so that [benefit]
**Priority**: Must Have / Should Have / Could Have
**Story Points**: [N]

**Acceptance Criteria**:
- **Given** [context] **When** [action] **Then** [outcome]

**Technical Notes**: [implementation considerations]

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] Documentation updated

**Dependencies**: [other story IDs]
**Risk Level**: High / Medium / Low
```

## NFR File Template

```markdown
# Non-Functional Requirements

## Overview
**Total Stories**: [N] | **Total Points**: [N]

## Performance Requirements
### Story NFR-PERF-001: [Title]

## Security Requirements
### Story NFR-SEC-001: [Title]

## Accessibility Requirements
### Story NFR-ACC-001: [Title]

## Integration Requirements
### Story NFR-INT-001: [Title]

## Infrastructure Requirements
### Story NFR-INF-001: [Title]
```

## Story Point Distribution

| Points | Complexity | Description |
|--------|-----------|-------------|
| 1 | Trivial | Config change, simple fix |
| 2 | Simple | One component, clear scope |
| 3 | Medium | Multiple components, moderate logic |
| 5 | Complex | Cross-cutting, significant logic |
| 8 | Very Complex | Architecture change, high risk |
| 13 | Epic-level | Should be broken down further |
