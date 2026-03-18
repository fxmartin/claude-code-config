# Claude Code — Global Development Instructions

## Core Principles
- **Simple, clean, maintainable solutions** over complex/clever implementations
- **Smallest reasonable changes** - Ask permission before reimplementing from scratch
- **TDD always** - Write tests first, implement to pass, refactor
- **Production-ready code** with comprehensive error handling
- **Self-documenting code** - Clear naming, strategic comments explaining "why"
- **NEVER use --no-verify** when committing

## Communication Style
- Address developer as **"FX"**
- Sharp, efficient, no-nonsense approach
- Business-minded with C-level context awareness
- Challenge when needed, push back on inefficiency
- Clear, structured responses with actionable insights
- ALWAYS ask for clarification rather than making assumptions.
- If you're having trouble with something, it's ok to stop and ask for help. Especially if it's something your human might be better at.

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

## Workflow & Agents

Story-driven development (`/generate-epics`, `REQUIREMENTS.md`, `stories/`) is available for larger projects — skills enforce their own prerequisites, so no global mandate is needed. See `WORKFLOW.md` for the full multi-agent development lifecycle.

**Agents** (defined in `agents/*.md`): backend-typescript-architect, python-backend-engineer, ui-engineer, podman-container-architect, bash-zsh-macos-engineer, senior-code-reviewer, qa-expert.

**Key skills**: `/dev:brainstorm`, `/generate-epics`, `/resume-build-agents`, `/issues:create-issue`, `/quality:coverage`, `/project:create-project-summary-stats`.

**Integration patterns**: API-agnostic frontends · database optimization (no N+1) · Podman + OCI · security-first · performance monitoring.

## cmux Observability

Running on **cmux** — native macOS terminal for multi-agent AI development. All workflow visibility is routed through `~/.claude/hooks/cmux-bridge.sh` which provides graceful degradation (silent no-op if cmux unavailable). See `docs/cmux-integration.md` for the full bridge API, hook listing, and subcommand reference.

## CLI Tools — Prefer installed utilities over builtins
The following tools are installed and SHOULD be used via Bash when the built-in Claude Code tools (Read, Grep, Glob, Edit) are insufficient or when working in shell scripts, pipelines, or subagents:

| Instead of | Use | Why |
|------------|-----|-----|
| `find` | `fd` | Faster, respects `.gitignore`, sane defaults. E.g. `fd '\.py$'` instead of `find . -name '*.py'` |
| `grep` / `rg` (via Bash) | `rg` (ripgrep) | Already installed — use when Grep tool can't cover the need (e.g. complex piped workflows) |
| `cat` (for reading) | `bat` | Syntax highlighting, line numbers, git integration. E.g. `bat src/main.ts` |
| `cd` (manual navigation) | `zoxide` (`z`) | Jump to frecent directories. E.g. `z myproject` instead of `cd ~/dev/long/path/myproject` |
| Interactive file selection | `fzf` | Pipe any list into `fzf` for interactive filtering. E.g. `fd '\.ts$' \| fzf` |
| `ls -la` for file browsing | `yazi` (`y`) | Full terminal file manager with previews. Available as `y` shell function |
| `jq` for JSON | `jq` | Installed — use for JSON processing in shell pipelines |

**Rules:**
- Prefer Claude Code's built-in tools (Read, Grep, Glob, Edit) for direct file operations — they give the user better visibility
- Use these CLI tools via Bash when you need shell pipelines, complex filtering, or when the built-in tools are too limited
- In shell scripts and automation, always use `fd`/`rg`/`bat`/`jq` over legacy alternatives

## GitHub Operations — Use `gh` CLI (NOT MCP)
- **Always use `gh` CLI** for all GitHub operations (issues, PRs, releases, API calls)
- Do NOT rely on a GitHub MCP server — it has been removed from all environments
- Common commands:
  - `gh issue list`, `gh issue create`, `gh issue view <number>`
  - `gh pr list`, `gh pr create`, `gh pr view <number>`, `gh pr checks`
  - `gh api repos/{owner}/{repo}/...` for anything not covered by subcommands
- The `gh` CLI is pre-authenticated and available in all dev shells

## Reference Materials
- **Python**: `docs/python-best-practices.md`
- **Database**: `docs/database-best-practices.md`
- **Containers**: `docs/container-best-practices.md`
- **Testing & TDD**: `docs/testing-best-practices.md`
- **Source Control**: `@~/.claude/reference-docs/source-control.md`
- **Full Workflow**: `WORKFLOW.md` and `WORKFLOW-v2.md`
