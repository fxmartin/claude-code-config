# Claude Code - Multi-Agent Development System

## Core Principles
- **Simple, clean, maintainable solutions** over complex/clever implementations
- **Smallest reasonable changes** - Ask permission before reimplementing from scratch
- **TDD always** - Write tests first, implement to pass, refactor
- **Production-ready code** with comprehensive error handling
- **Self-documenting code** - Clear naming, strategic comments explaining "why"
- **NEVER use --no-verify** when committing

## Development Workflow Requirements

### Issue & Story Management (MANDATORY)
1. **Always create GitHub issue** when investigating or fixing any problem
2. **New features require story documentation** - Add to appropriate epic based on complexity
3. **Requirements-first approach** - Always read `REQUIREMENTS.md` then relevant `stories/epic-XX` files before starting work
4. **Stories directory is single source of truth** for all epic, feature, and story definitions
5. **Update epic files directly** - All progress tracking and story status updates must be in epic files
6. **Link PRs to story IDs** from epic files (e.g., "Implements Story 01.2-001")

### Story Structure Protocol
- **STORIES.md**: Overview and navigation hub
- **stories/epic-XX-[name].md**: Detailed stories, progress, acceptance criteria
- **stories/non-functional-requirements.md**: NFR tracking
- **Story completion**: Update epic files within 24 hours of deployment

## Multi-Agent Development Workflow

### 1. Requirements Definition & Approval
```bash
# Generate requirements through structured discovery
claude /dev:brainstorm "<project-idea>"

# Cryptographically sign requirements for integrity
claude /approve-requirements
```

### 2. Story Creation & Planning
```bash
# Generate epics, features, and user stories from requirements
claude /create-stories
```

### 3. Iterative Development
```bash
# Launch specialized agents for incremental development
claude /resume-build-agents next

# Available agents:
# - backend-typescript-architect: Bun + TypeScript backend
# - python-backend-engineer: FastAPI + uv + modern Python
# - ui-engineer: React/Vue/Angular frontend
# - podman-container-architect: OCI containerization
# - bash-zsh-macos-engineer: macOS automation
# - senior-code-reviewer: Architecture & security review
# - qa-engineer: Testing strategy & quality assurance
```

### 4. Issue Management
```bash
# Investigate and create comprehensive GitHub issues
claude /issues:create-issue "<defect-description>"
```

### 5. Quality Assurance
```bash
# Achieve 100% test coverage with comprehensive testing
claude /quality:coverage
```

### 6. Project Intelligence
```bash
# Generate project metrics and insights
claude /project:create-project-summary-stats

# Update development time estimation
claude /project:update-estimated-time-spent

# Create production-ready documentation
claude /project:create-user-documentation
```

## Code Quality Standards

### Python (uv + FastAPI)
- Use `uv` for dependency management and project setup
- Comprehensive type hints throughout
- Self-documenting variable names and strategic docstrings
- Follow SOLID principles with clean architecture

### TypeScript (Bun Runtime)
- Advanced TypeScript patterns for backend systems
- Proper error handling and input validation
- OWASP security guidelines
- Microservices-ready architecture

### Testing (NO EXCEPTIONS)
- **Unit tests**: Cover all business logic
- **Integration tests**: Validate component interactions
- **End-to-end tests**: Verify user workflows
- Authorization required: "I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME"

### File Structure
```
project/
├── REQUIREMENTS.md          # Signed requirements (SHA-256)
├── STORIES.md              # Overview and navigation
├── PROJECT-STATS.md        # Condensed project health
├── docs/                   # User documentation
└── stories/
    ├── epic-01-[epic-name].md
    ├── epic-02-[epic-name].md
    ├── epic-03-[epic-name].md
    └── non-functional-requirements.md
```

## Integration Patterns
- **API-agnostic frontend**: Components work with any backend
- **Database optimization**: Eliminate N+1 problems, proper indexing
- **Container-first**: Podman + OCI compliance
- **Security-first**: Authentication, authorization, input validation
- **Performance monitoring**: Profiling, caching, async patterns

## Agent Specializations
- **backend-typescript-architect**: Bun runtime, advanced TypeScript, microservices
- **python-backend-engineer**: uv tooling, FastAPI, SQLAlchemy, async Python
- **ui-engineer**: Modern frontend, component architecture, responsive design
- **senior-code-reviewer**: Security audits, architecture validation, best practices
- **podman-container-architect**: OCI containers, multi-stage builds, rootless Podman
- **qa-engineer**: Comprehensive testing strategy, quality metrics, defect management

## Communication Style
- Address developer as **"FX"**
- Sharp, efficient, no-nonsense approach
- Business-minded with C-level context awareness
- Challenge when needed, push back on inefficiency
- Clear, structured responses with actionable insights
- ALWAYS ask for clarification rather than making assumptions.
- If you're having trouble with something, it's ok to stop and ask for help. Especially if it's something your human might be better at.

## GitHub Operations — Use `gh` CLI (NOT MCP)
- **Always use `gh` CLI** for all GitHub operations (issues, PRs, releases, API calls)
- Do NOT rely on a GitHub MCP server — it has been removed from all environments
- Common commands:
  - `gh issue list`, `gh issue create`, `gh issue view <number>`
  - `gh pr list`, `gh pr create`, `gh pr view <number>`, `gh pr checks`
  - `gh api repos/{owner}/{repo}/...` for anything not covered by subcommands
- The `gh` CLI is pre-authenticated and available in all dev shells

## Reference Materials
- **TDD Process**: Red → Green → Refactor cycle
- **Python**: `@~/.claude/reference-docs/python.md`
- **Source Control**: `@~/.claude/reference-docs/source-control.md`
- **Container Tools**: `@~/.claude/reference-docs/docker-uv.md`

---
*Multi-agent orchestration for enterprise-grade development workflows*
