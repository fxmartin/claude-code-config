# Agent Coordination Rules

## Agent Selection Logic

### By Story Type
- **API/Backend Logic** → `backend-typescript-architect` or `python-backend-engineer`
- **Frontend/UI** → `ui-engineer`
- **Database/Data Processing** → `python-backend-engineer`
- **Architecture/System Design** → `backend-typescript-architect`
- **Code Quality/Refactoring** → `senior-code-reviewer`
- **Automation/DevOps/Scripting** → `bash-zsh-macos-engineer`
- **Testing/QA** → `qa-engineer`
- **Containerization/Deployment** → `podman-container-architect`

### By Technology Stack Detection
- TypeScript/Node.js files → `backend-typescript-architect`
- Python/FastAPI files → `python-backend-engineer`
- React/Frontend files → `ui-engineer`
- Shell scripts (.sh/.zsh/.bash) → `bash-zsh-macos-engineer`
- Test files (.test.js/.spec.ts/.test.py) → `qa-engineer`
- Container files (Dockerfile, Containerfile) → `podman-container-architect`
- CI/CD files (.github/workflows) → `bash-zsh-macos-engineer`

### Supporting Agent Rules
- **Always include `senior-code-reviewer`** for final review phase
- **Always include `qa-engineer`** for quality validation
- **Cross-stack stories** require multiple agents
- **Full-stack features** need backend + frontend coordination
- **DevOps stories** need `bash-zsh-macos-engineer` + application agents

## Argument Validation

- `$ARGUMENTS="next"`: Parse STORIES.md for highest priority unblocked story
- `$ARGUMENTS` is story-id: Validate it exists and is actionable
- `$ARGUMENTS` is epic-name: Find next story within that epic
- STOP if target story is: DONE, BLOCKED, or missing acceptance criteria

## Branch Management

- Extract clean story ID (e.g., US-001, EPIC-02-AUTH)
- If branch `feature/$STORY_ID` exists: checkout and rebase on main
- If new: `git checkout -b feature/$STORY_ID` from latest main

## Discovery Phase

1. Read STORIES.md for epic structure
2. Read relevant `docs/stories/epic-XX-*.md` for story details
3. Check existing progress: `git log --oneline origin/main..HEAD`
4. Agent-specific investigation:
   - Backend agents: API routes, services, middleware
   - Frontend agents: Components, pages, hooks
   - DevOps agents: Scripts, CI/CD, deployment
   - QA agents: Test coverage, frameworks, quality metrics

## Development Phase (TDD)

### Phase 1: Architecture & Planning
Design API contracts, component architecture, or automation workflows.

### Phase 2: Test-First
Write failing tests that define expected behavior from acceptance criteria.

### Phase 3: Implementation
Follow Red → Green → Refactor. Implement minimal code to pass tests.

### Phase 4: Code Review (MANDATORY)
`senior-code-reviewer` reviews for security, performance, maintainability.

### Phase 5: Integration Testing
Cross-layer validation, E2E testing, performance testing.

## Cross-Agent Collaboration Examples

### Full-Stack Feature
1. Backend agent: Implement API
2. Frontend agent: Implement UI
3. Both ensure contract compatibility
4. QA agent validates integration
5. Reviewer approves

### DevOps Integration
1. bash-zsh-macos-engineer: Implement automation
2. Backend agents: Provide integration points
3. QA agent validates deployment
4. Reviewer ensures security

### Containerization
1. podman-container-architect: Design container architecture
2. Application agents: Ensure compatibility
3. bash-zsh-macos-engineer: Deployment automation
4. QA agent: Container testing

## Error Handling

- **Agent expertise mismatch**: Auto-reassign to appropriate agent
- **Cross-agent conflicts**: `senior-code-reviewer` mediates
- **Agent failure**: Fallback to general implementation, flag for review

## Smart Boundaries

- Stories estimated >8 hours: Suggest breaking down
- Investigation >30min without progress: Document and pause
- Implement only acceptance criteria scope
- Note "nice to have" improvements as separate stories

## Commit Strategy

```
feat(epic-name): implement [story description] (#STORY-ID)

- Add [functionality 1]
- Implement [functionality 2]

Acceptance criteria:
- [x] Criteria 1
- [x] Criteria 2

Refs: #STORY-ID
```

## PR Template

```bash
gh pr create \
  --title "feat: [Story Title] (#STORY-ID)" \
  --body "## Summary
Implements [description]

## Agent Contributions
- **Primary**: [agent] - [implementation]
- **Review**: senior-code-reviewer - [quality gates]

## Testing
- Unit: [coverage]
- Integration: [validation]

Closes #STORY-ID"
```
