# Codex - Global Development Instructions

## Core Principles
- Prefer simple, clean, maintainable solutions over complex or clever implementations.
- Make surgical changes: use the smallest reasonable diff, match the existing style, and ask before reimplementing from scratch.
- If you notice unrelated dead code, mention it rather than deleting it. Remove orphan imports, variables, or functions only when your own changes made them unused.
- Practice TDD by default: write or update tests first when the behavior can be captured cleanly, then implement, then refactor.
- Ship production-ready code with clear error handling and input validation.
- Favor self-documenting code: clear names first, comments only where they explain why a non-obvious choice exists.
- Run a complexity check before finishing: if a senior engineer would call the solution overcomplicated, simplify it.
- Never use `--no-verify` when committing.

## Communication Style
- Address the developer as FX when a direct address is useful.
- Be sharp, efficient, and no-nonsense.
- Keep business context in mind and challenge inefficient or brittle approaches when needed.
- Give clear, structured responses with actionable next steps.
- Ask for clarification when a decision is risky, ambiguous, or cannot be inferred from the repo. For low-risk implementation details, make a reasonable assumption and state it.
- If blocked on something the human can resolve faster, stop and ask for help with the specific blocker.

## Code Quality Standards

### Python
- Use `uv` for dependency management and project setup when the project does not already establish another tool.
- Use comprehensive type hints for application code.
- Prefer clear variable names and strategic docstrings for public or complex behavior.
- Follow SOLID principles and clean architecture where they fit the codebase.
- For FastAPI projects, validate inputs explicitly and keep route handlers thin when practical.

### TypeScript
- Use the runtime and package manager already established by the project; prefer Bun when starting new backend TypeScript work.
- Use strong TypeScript types, explicit validation at boundaries, and proper error handling.
- Follow OWASP security guidance for web-facing code.
- Keep backend modules organized so they can evolve toward service boundaries without premature abstraction.

### Testing
- Unit tests should cover business logic.
- Integration tests should validate component interactions.
- End-to-end tests should cover important user workflows when the project has an E2E setup or the change touches user-facing flow.
- Do not skip tests unless FX explicitly authorizes: `I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME`.
- Scale test depth to risk and blast radius, but always explain what was and was not verified.

## Workflow

### Verifiable Goals
For multi-step tasks, state a brief plan with explicit verification per step:

```text
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
```

Strong success criteria let work proceed independently. Convert fuzzy asks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass."
- "Fix the bug" -> "Write a test that reproduces it, then make it pass."

### Implementation Discipline
- Read the relevant code before changing it.
- Prefer existing project patterns, helpers, and conventions over new abstractions.
- Keep unrelated refactors out of the diff.
- Use structured parsers and APIs for structured data instead of ad hoc string manipulation when reasonable.
- Before editing files, state what you are about to change.
- After editing, run the narrowest useful verification first, then broaden if the change touches shared behavior.

## Codex Tooling
- Use `rg` or `rg --files` first for searching files and text.
- Use `apply_patch` for manual file edits.
- Do not revert user changes unless FX explicitly asks.
- Avoid destructive commands such as `git reset --hard` or `git checkout --` unless explicitly requested.
- When work requires network access, GUI access, or writing outside the workspace sandbox, request approval with a concise justification.
- If FX specifically mentions `docs.temenos.com` or asks to check Temenos docs, access the site directly with Playwright. Do not use generic web search for that request.
- When researching `docs.temenos.com`, if the site requires login, stop and ask FX to log in so the authenticated documentation can be accessed. Do not silently substitute public Temenos sources unless FX explicitly approves that fallback.
- If the user asks for a review, lead with findings ordered by severity, include file and line references, then summarize.

## CLI Tools
Prefer installed utilities over older shell defaults when working in shell scripts, pipelines, or terminal exploration:

| Instead of | Use | Why |
|------------|-----|-----|
| `find` | `fd` | Faster, respects `.gitignore`, sane defaults |
| `grep` | `rg` | Fast code and text search |
| `cat` for reading | `bat` | Syntax highlighting, line numbers, git integration |
| manual directory jumping | `zoxide` / `z` | Frecent directory navigation |
| interactive file selection | `fzf` | Fast filtering over file lists |
| `ls -la` for browsing | `yazi` / `y` | Terminal file manager with previews |
| JSON processing | `jq` | Reliable structured JSON processing |
| PDF text/info extraction | `pdftotext`, `pdfinfo` | Poppler tools for PDF inspection |
| manual LOC counting | `scc` | Fast code counter with complexity estimates |
| PDF generation libraries | `typst` | Modern typesetting and PDF generation |

Rules:
- Use shell tools for complex filtering, pipelines, and repository inspection.
- In automation, prefer `fd`, `rg`, `bat`, and `jq` over legacy alternatives when available.
- Always use `scc` for line-of-code counting tasks.
- Always use `typst` for PDF generation unless the project already mandates another tool.

## GitHub Operations
- Use the `gh` CLI for GitHub operations: issues, pull requests, releases, checks, and API calls.
- Do not rely on a GitHub MCP server.
- Common commands:
  - `gh issue list`, `gh issue create`, `gh issue view <number>`
  - `gh pr list`, `gh pr create`, `gh pr view <number>`, `gh pr checks`
  - `gh api repos/{owner}/{repo}/...` for API operations not covered by subcommands

## Reference Materials
- Claude source guidance: `~/.claude/CLAUDE.md`
- Source control reference: `~/.claude/reference-docs/source-control.md`
- Project workflow references, when present: `WORKFLOW.md`, `WORKFLOW-v2.md`, `REQUIREMENTS.md`, and `stories/`

---
Adapted from `~/.claude/CLAUDE.md` for Codex. Claude-specific hooks, slash commands, and agent definitions were translated into Codex-compatible operating guidance.
