# From CLAUDE.md to 496 PRs: Progressively Mastering Every Claude Code Mechanism

> **Audience**: Developers and architects exploring AI-driven development
> **Tone**: Punchy, confident, data-backed — show the progressive build-up
> **Deck length**: 18 slides

---

## Slide 1 — One Developer, 496 PRs, 1,180 Commits: Built by Orchestrating Claude Code

A single developer. Two production applications. **1,180 commits** in under a month.

Not by coding faster — by progressively stacking every Claude Code mechanism into an autonomous development machine.

**CLAUDE.md → Agents → Skills → Commands → Hooks → Worktrees → cmux → GitHub at every step.**

This deck shows how each mechanism was adopted, why it matters, and the real numbers it produced.

---

## Slide 2 — The Journey: From Single-File Config to Full Orchestration

The system wasn't built in a day. Each Claude Code mechanism solved a specific pain point, and they compound on each other.

| Layer | Mechanism | What It Solved |
|-------|-----------|---------------|
| 1 | **CLAUDE.md** | Consistent standards across sessions |
| 2 | **Agents** | Specialized expertise per domain |
| 3 | **Skills** | Repeatable multi-step workflows |
| 4 | **Commands** | Quick-access operations |
| 5 | **Hooks** | Automated lifecycle events |
| 6 | **Git worktrees** | Parallel agent isolation |
| 7 | **cmux bridge** | Real-time observability |
| 8 | **GitHub (gh CLI)** | Version control at every step |

Each layer builds on the previous one. Remove any layer and the system degrades.

---

## Slide 3 — Layer 1: CLAUDE.md — The Constitution Every Agent Reads

CLAUDE.md is the foundation. It's the first file every Claude Code session loads — the global instructions that shape all behavior.

**What ours enforces:**
- Address the developer as "FX" — sharp, no-nonsense tone
- TDD always (skip requires explicit authorization string)
- Never use `--no-verify` on commits
- Python uses `uv` + FastAPI; TypeScript uses Bun runtime
- Use `fd` over `find`, `bat` over `cat`, `scc` for LOC counting, `typst` for PDF generation
- All GitHub operations via `gh` CLI (no MCP)

**Why it matters:** Without CLAUDE.md, every session starts from zero. With it, every agent inherits the same standards, tooling preferences, and quality bar — automatically.

---

## Slide 4 — Layer 2: Agents — Specialized Experts, Not Generic Assistants

Instead of one generalist, we defined **12 specialized agents** — each with a system prompt, tool restrictions, and domain expertise.

**Core development agents:**

| Agent | Domain | Key Expertise |
|-------|--------|---------------|
| **backend-typescript-architect** | Bun, APIs, microservices | Database optimization, auth, N+1 prevention |
| **python-backend-engineer** | FastAPI, SQLAlchemy, async | Pydantic models, uv dependency management |
| **ui-engineer** | React, components, accessibility | CSS-in-JS, state management, responsive design |
| **bash-zsh-macos-engineer** | Shell, CI/CD, DevOps | macOS automation, launchd, Homebrew |
| **podman-container-architect** | OCI, multi-stage builds | Rootless Podman, Python+uv container patterns |
| **qa-engineer** | Test strategy, coverage | Defect management, automation > 70% target |
| **senior-code-reviewer** | Architecture, security | 15+ years experience, OWASP, mandatory gate |

**How they're used:** The orchestrator reads each story's tags and tech stack, then auto-selects the right agent. A FastAPI endpoint story goes to `python-backend-engineer`. A React component goes to `ui-engineer`. Code review always goes to `senior-code-reviewer` — no exceptions.

---

## Slide 5 — Layer 3: Skills — Repeatable Workflows That Orchestrate Agents

Skills are the multi-step workflows that chain agents, tools, and quality gates into reproducible pipelines. **16 skills** organized by domain.

**The core pipeline — 6 skills that build software end-to-end:**

**`/project-init`** → Bootstrap repo with CLAUDE.md, PROJECT-SEED.md, CI/CD config

**`/brainstorm`** → PM-style 8-question interview → REQUIREMENTS.md

**`/generate-epics`** → Requirements → AGILE epic files with INVEST-compliant user stories

**`/build-stories`** → Multi-story autonomous build with TDD, coverage, review, merge

**`/create-issue`** → Structured GitHub issue creation with severity and labels

**`/fix-issue`** → 11-phase autonomous pipeline from investigation to merge

**Each skill declares:**
- `allowed-tools` — prevents accidental tool sprawl
- `argument-hint` — CLI completion for parameters
- Supporting files — `quality-gates.md`, `build-prompt.md`, `agent-coordination.md`

---

## Slide 6 — Layer 4: Commands — 17 Slash Commands for Quick Operations

Commands are lightweight actions — single-purpose tools organized into 6 categories.

| Category | Commands | Examples |
|----------|----------|---------|
| **Quality** | 3 | `/quality/roast` (brutal code review), `/quality/coverage`, `/quality/project-review` |
| **Project** | 5 | `/project/update-progress`, `/project/create-project-summary-stats`, `/project/create-user-documentation` |
| **Issues** | 1 | `/issues/create-issue` (comprehensive defect analysis) |
| **DevOps** | 2 | `/devops/check-releases`, `/devops/plan-release-update` |
| **Research** | 3 | `/research/crypto-analysis`, `/research/profile-analysis`, `/research/client-analysis` |
| **Generators** | 1 | `/backfill-docs` (documentation for existing code) |

**Skills vs. Commands:** Skills orchestrate multi-step workflows with agents and quality gates. Commands execute a focused action — check something, generate something, update something. Both are invoked with `/` but serve different scales of work.

---

## Slide 7 — Layer 5: Hooks — Automated Lifecycle Events

Hooks fire automatically on Claude Code lifecycle events — no manual invocation needed. **7 hook scripts** across 5 event types.

| Event | Hook Script | What It Does |
|-------|------------|-------------|
| **SessionStart** | `cmux-session-start.sh` | Logs "session started", sets blue "Ready" status pill |
| **SubagentStart** | `cmux-agent-start.sh` | Shows "Running: {agent_type}" pill (suppressed during batch builds) |
| **SubagentStop** | `cmux-agent-stop.sh` | Clears pill, logs completion, fires desktop notification |
| **Stop** | `cmux-stop.sh` | Clears all progress, "Claude Done" notification |
| **Notification** | `cmux-permission.sh` | Red "Permission Needed" pill + urgent notification |
| **PostToolUse** | `on-pr-merge-docs.sh` | After `gh pr merge`, injects context to trigger doc updates |

**The PR merge hook is key:** Every time a PR merges, the hook injects `additionalContext` that prompts Claude to update README, story progress, and project documentation. Documentation stays current without manual effort.

**Sentinel file pattern:** `/build-stories` writes `/tmp/.claude-skill-active` so agent-level hooks stay quiet during batch runs — only the orchestrator reports status.

---

## Slide 8 — Layer 6: Git Worktrees — Parallel Agents Without File Conflicts

The breakthrough for parallel execution: **git worktrees** give each agent an isolated copy of the repo while sharing the same `.git` directory.

**Without worktrees:** Agents overwrite each other's files. One agent at a time.

**With worktrees:** Up to 5 agents build concurrently in separate directory trees. Branches are visible across worktrees because they share `.git`.

**The parallel build pipeline:**

| Stage | Agents | Isolation | Concurrency |
|-------|--------|-----------|-------------|
| Build (TDD) | Specialized per story | Worktree | Up to 5 |
| Coverage gate | qa-engineer | Worktree | Up to 5 |
| Code review | senior-code-reviewer | None needed (read-only) | Up to 5 |
| Merge | Orchestrator | None (sequential + rebase) | 1 |

**Critical rule:** Build agents always `git push` before returning — the coverage agent in another worktree needs to `git fetch` the branch.

---

## Slide 9 — Layer 7: cmux Bridge — Real-Time Observability for Multi-Agent Runs

**cmux** is a native macOS terminal built for multi-agent AI development. Our bridge script (`cmux-bridge.sh`, 105 lines) provides 8 subcommands:

**Status pills** — Color-coded phase indicators (blue=running, green=done, red=failed)

**Progress bars** — Fraction complete from 0.0 to 1.0, updated per phase or per story

**Sidebar logs** — Permanent event ledger: "Agent started: python-backend-engineer", "Coverage: 94%", "PR #287 merged"

**Desktop notifications** — Milestones only: preflight failure, E2E gate results, build completion

**Telegram fallback** — Envelope-based alerts (start, first failure, abort, finish) via Bot API. Rate-limited: one failure alert per run.

**Pane management** — Parallel mode auto-creates split panes per cohort, labeled with story IDs. Auto-closed when cohort completes.

**Graceful degradation:** If cmux is not running, every hook silently no-ops. The workflow works identically — you just don't see the sidebar.

---

## Slide 10 — Layer 8: GitHub at Every Step — gh CLI as the Backbone

GitHub is not an afterthought — it's integrated at **every single step** of the workflow via the `gh` CLI.

| Workflow Step | GitHub Operation | Command |
|--------------|-----------------|---------|
| **Pre-flight** | Verify clean git state + auth | `gh auth status` |
| **Build** | Create feature branch | `git checkout -b feature/{ID}` |
| **Coverage** | Push branch + create PR | `git push -u origin` → `gh pr create` |
| **Review** | Comment on PR | `gh pr review` |
| **Merge** | Merge PR + trigger hook | `gh pr merge` → `on-pr-merge-docs.sh` |
| **Issue create** | Structured issue | `gh issue create --label bug,high --body ...` |
| **Issue fix** | Fetch + investigate + close | `gh issue view` → fix → `gh issue close` |
| **E2E gate** | Check status | `gh pr checks` |
| **Progress** | Link PRs to stories | PR numbers tracked in `.build-progress.md` |

**No GitHub MCP.** The `gh` CLI is pre-authenticated, runs in any shell, and never hits token limits. Every PR links to a story ID. Every issue links to a fix branch. Full traceability.

---

## Slide 11 — The Complete Pipeline: From Blank Repo to Production

**Phase 1: Discovery**
`/project-init` → `/brainstorm` → **REQUIREMENTS.md** committed to GitHub

**Phase 2: Planning**
`/generate-epics` or `/create-epic` → **Epic files + STORIES.md** committed to GitHub

**Phase 3: Build (Two Paths)**
- `/resume-build-agents next` — One story at a time, full control
- `/build-stories all --parallel` — All stories, autonomous, worktree-isolated

**Per story:** TDD → Build → Coverage (90%+) → Review → PR → Merge → Progress update

**Phase 4: Quality**
`/fix-issue all` → `/design-e2e` → `/execute-e2e-tests` → `/coverage` → `/project-review`

**Every phase produces GitHub artifacts** — commits, branches, PRs, issues, reviews. Nothing lives only on disk.

---

## Slide 12 — Deep Dive: /build-stories — The 7-Phase Orchestrator

The most complex skill in the system. **Thin dispatcher** that never reads source code — only coordinates.

**Phase 1:** Parse arguments, validate environment (clean git, `gh auth`, main branch)

**Phase 2:** Discovery Agent parses STORIES.md → topological sort → build queue (JSON)

**Phase 3:** Queue parsing with `--limit=N` and dependency integrity checks

**Phase 4:** Organize into dependency cohorts (stories with no unmet deps run first)

**Phase 5:** Per-story loop — Build → Coverage → Review → Merge → Bugfix loop if needed

**Phase 6:** Summary agent generates batch report + documentation update agent

**Phase 7:** Print report, desktop notification, Telegram alert, cleanup sentinel file

**Flags:** `--parallel` (default), `--sequential`, `--dry-run`, `--skip-coverage`, `--auto`, `--e2e-gate=block|warn|off`, `--limit=N`

---

## Slide 13 — Deep Dive: /fix-issue — 11-Phase Autonomous Bug Resolution

| Phase | Progress | What Happens |
|-------|----------|-------------|
| 1 | 0.09 | Validate issue number, check git state, pre-flight tests |
| 2 | 0.18 | Fetch issue from GitHub via `gh issue view` |
| 3 | 0.27 | Investigation agent: root cause analysis (read-only) |
| 4 | 0.36 | Build agent: TDD fix on `fix/issue-{N}-{slug}` branch |
| 5 | 0.45 | Coverage gate: 90%+ or bugfix loop |
| 6 | 0.64 | Code review: senior-code-reviewer validates |
| 7 | 0.73 | E2E tests (if enabled) |
| 8 | — | Bugfix loop: classify (CODE_BUG/TEST_BUG/ENV_ISSUE), retry max 2x |
| 9 | 0.82 | Merge PR via `gh pr merge`, close issue via `gh issue close` |
| 10 | 0.91 | Summary: generate fix report |
| 11 | 1.0 | Notification: desktop + Telegram |

**Batch mode:** `--limit=5` runs 5 issues in parallel with **file-overlap guard** — issues touching the same files are auto-serialized.

---

## Slide 14 — The Quality Gates: No Shortcuts, No Exceptions

Every piece of code passes through **4 mandatory gates** before reaching main:

**Gate 1: TDD Cycle** — Tests are written first. Code is implemented to pass. Then refactored. Red → Green → Refactor on every story.

**Gate 2: Coverage (90%+)** — The qa-engineer agent verifies branch coverage. Below threshold triggers the bugfix loop to add missing tests.

**Gate 3: Code Review** — The `senior-code-reviewer` agent checks architecture, security (OWASP), performance, naming, test quality. Approves or requests changes.

**Gate 4: E2E Tests** — After all stories in an epic merge, Playwright end-to-end tests validate user workflows. Three modes: `block` (stop on fail), `warn` (log only), `off` (skip).

**Bugfix loop (max 2 retries):** Classifies failures as CODE_BUG, TEST_BUG, or ENV_ISSUE. Creates GitHub issue for CODE_BUG. Auto-fixes and retests. If unfixed after 2 tries: mark FAILED, alert, continue or prompt.

---

## Slide 15 — MCP Servers & CLI Tools: The Supporting Infrastructure

**MCP Servers (2):**
- **Playwright** — Browser automation for E2E test design and execution (`/design-e2e`, `/execute-e2e-tests`)
- **Context7** — Upstash context management for documentation retrieval

**CLI Tools mandated in CLAUDE.md (9 tools):**

| Tool | Replaces | Why |
|------|----------|-----|
| `fd` | `find` | 10x faster, respects .gitignore |
| `rg` (ripgrep) | `grep` | Already the best |
| `bat` | `cat` | Syntax highlighting, git integration |
| `zoxide` | `cd` | Jump to frecent directories |
| `yazi` | `ls -la` | Full terminal file manager |
| `jq` | — | JSON processing in pipelines |
| `scc` | `wc -l` | Code counter with complexity estimates |
| `typst` | reportlab/weasyprint | Modern typesetting to PDF |
| `pdftotext` | — | PDF text extraction (Poppler) |

**Templates directory:** Scaffolding for new agents, skills, and commands — so the system can extend itself.

---

## Slide 16 — The Numbers: Two Projects, Real Production Code

| Metric | infobasic-bench | forge | Combined |
|--------|----------------|-------|----------|
| **Commits** | 508 | 672 | **1,180** |
| **Merged PRs** | 216 | 280 | **496** |
| **Issues resolved** | 93 | 67 | **160** |
| **Epics** | 30 | 25 | **55** |
| **Stories** | — | 241 (227 done) | **94% autonomous** |
| **Languages** | Python 64% / TS 31% | Python 65% / TS 35% | Full-stack |
| **Timeline** | ~4 weeks | 18 days | — |
| **Agents used** | 7 | 7 | **12 defined** |
| **Skills** | 16 | 16 | **16** |
| **Hooks** | 7 | 7 | **7** |

Both are **full-stack applications**: Python backends (FastAPI), TypeScript frontends (React), PostgreSQL, Docker, Alembic migrations, Playwright E2E tests, and comprehensive unit test suites.

---

## Slide 17 — What We Built: infobasic-bench & forge

**infobasic-bench** — A core banking code analysis and benchmarking platform. Evaluates InfoBasic/jBC code quality across multiple LLM models, with scoring dimensions, cost tracking, and benchmark reports. Used internally to assess Temenos T24 modernisation approaches.

**forge** — A full Temenos T24 assessment and modernisation platform. Ingests client code extracts, maps dependencies, runs LLM-powered analysis pipelines, generates migration specifications, and produces deliverables (TAFJ migration reports, test case foundations, assessment reports). Multi-tenant with client/programme/instance hierarchy, role-based access control, and a glassmorphism UI design system.

**Both projects were built almost entirely by AI agents** — from the first Docker Compose file to the last Playwright E2E test.

---

## Slide 18 — The Compound Effect: Stack the Mechanisms, Multiply the Output

Each Claude Code mechanism alone is useful. **Stacked together, they're transformational.**

**CLAUDE.md** ensures consistency. **Agents** provide expertise. **Skills** chain them into pipelines. **Commands** handle quick ops. **Hooks** automate lifecycle events. **Worktrees** unlock parallelism. **cmux** provides visibility. **GitHub** provides traceability.

**The result:** A solo developer shipping at the pace of a 10-person team — with code review on every PR, 90%+ test coverage, and full audit trails.

**This is not the future of development. This is now.**

Start with CLAUDE.md. Add an agent. Build a skill. The compound effect will do the rest.
