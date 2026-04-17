# Claude Code — Global Development Instructions

## Core Principles
- **Simple, clean, maintainable solutions** over complex/clever implementations
- **Surgical changes** - smallest reasonable diff. Ask permission before reimplementing from scratch. Match existing style, even if you'd do it differently. If you notice unrelated dead code, mention it — don't delete it. Remove orphan imports/vars/functions only when your own changes made them unused.
- **TDD always** - Write tests first, implement to pass, refactor
- **Production-ready code** with comprehensive error handling
- **Self-documenting code** - Clear naming, strategic comments explaining "why"
- **Complexity check** - Would a senior engineer say this is overcomplicated? If yes, simplify.
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

### Verifiable goals
For multi-step tasks, state a brief plan with explicit verification per step:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Strong success criteria let agents loop independently. Weak criteria ("make it work") require constant clarification. Transform fuzzy asks into verifiable goals: "Add validation" → "Write tests for invalid inputs, then make them pass". "Fix the bug" → "Write a test that reproduces it, then make it pass".

**Agents** (defined in `agents/*.md`): backend-typescript-architect, python-backend-engineer, ui-engineer, podman-container-architect, bash-zsh-macos-engineer, senior-code-reviewer, qa-expert.

**Key skills**: `/brainstorm`, `/generate-epics`, `/resume-build-agents`, `/issues:create-issue`, `/quality:coverage`, `/project:create-project-summary-stats`.

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
| PDF text/info extraction | `pdftotext`, `pdfinfo` | Poppler utils — extract text, metadata, images from PDFs. E.g. `pdftotext doc.pdf -` for stdout |
| `wc -l`, manual LOC counting | `scc` | Fast code counter with complexity and COCOMO estimates. **Always use `scc` for any LOC counting task.** E.g. `scc .` or `scc src/` |
| PDF generation (`reportlab`, `weasyprint`, etc.) | `typst` | Modern typesetting → PDF. **Always use `typst` for PDF generation.** Write a `.typ` file then `typst compile file.typ`. No Python libs or venvs needed |

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
- **CLAUDE.md guide**: `docs/claude-md-guide.md` — structure, guardrails, maintenance, and how this file itself is organized
- **Python**: `docs/python-best-practices.md`
- **Database**: `docs/database-best-practices.md`
- **Containers**: `docs/container-best-practices.md`
- **Testing & TDD**: `docs/testing-best-practices.md`
- **Source Control**: `@~/.claude/reference-docs/source-control.md`
- **Full Workflow**: `WORKFLOW.md` and `WORKFLOW-v2.md`

---
*Surgical Changes rules, Complexity check, and Verifiable Goals template adapted from [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT), itself derived from Andrej Karpathy's observations on LLM coding pitfalls.*
