# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v1.8.0] - 2026-05-20

### Added

- feat(installer): wsl2 detection and platform-aware behavior (#3.1-002) (#27)


### Added

- WSL2 detection and platform-aware behavior in installer (#3.1-002)

## [v1.7.0] - 2026-05-20

### Added

- feat(state): define SQLite ledger schema and migration tooling (#4.1-001) (#25)


## [v1.6.0] - 2026-05-20

### Added

- feat(installer): split install.sh into --core/--tools/--mcp/--shell/--all modes (#3.1-001) (#26)
- SQLite ledger schema and migration tooling (Epic-04 foundation): a new
  `state/schema.sql` documents the canonical ledger shape (`runs`, `stories`,
  `stages`, `events`, `_migrations`), the first migration lives at
  `state/migrations/001-init.sql`, and `scripts/sdlc-state.sh` provides
  `init` / `migrate` / `show` / `prune --older-than` / `backup` subcommands
  over `sqlite3`. WAL journal mode is enabled at init. The DB path is
  configurable via `--db` (default `.sdlc-state.db`); the file is
  `.gitignore`d alongside its WAL companions. A bats suite
  (`tests/sdlc-state.bats`, 22 tests) covers fresh-DB init, idempotent
  re-migrate, schema introspection, composite primary keys, show, prune
  (with IN_PROGRESS run protection), and backup. Stories 4.2-001, 4.2-002,
  and 4.3-001 will build write helpers, a markdown renderer, and a resume
  subcommand on top of this. (#4.1-001)

### Changed

- `install.sh` is now a thin dispatcher over per-mode modules in `install/`.
  New flags `--core`, `--tools`, `--mcp`, `--shell`, and `--all` let you opt
  into exactly the parts of the framework you want. The default when no mode
  flag is passed is `--core` (symlinks only), a conservative, additive
  default. Every mode is idempotent and `--dry-run` now exactly previews the
  actions that the real run would perform — the dry-run drift around
  "Created ~/.claude" reported by Codex is fixed (`mkdir -p` now goes through
  the same `run` guard as everything else). `--mcp` normalises its JSON
  output through `jq` so a second run is byte-identical to the first.
  Backward-compatible: `--skip-mcp` (≡ `--core --tools --shell`) and
  `--skip-tools` (≡ `--core --mcp --shell`) still work but now emit a
  deprecation warning pointing at the new modes; both will be removed in the
  next MAJOR release. (#3.1-001)

## [v1.5.0] - 2026-05-19

### Added

- feat: changelog bootstrap and auto-maintenance (#5.3-001) (#24)


## [v1.4.0] - 2026-05-19

### Added

- Automatic release pipeline: a new `.github/workflows/release.yml` that, on
  every push to `main`, computes the next semantic version from the
  Conventional Commits since the last tag, aligns the `version` fields of
  `plugins/autonomous-sdlc/.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json`, prepends a CHANGELOG section, commits the
  bump as a `chore(release):` commit, tags it `vX.Y.Z`, and publishes a GitHub
  Release with auto-generated notes. Bump rules: `BREAKING CHANGE:`/`!` →
  MAJOR, `feat` → MINOR, `fix`/`perf`/`refactor` → PATCH, and a
  chore/docs-only push is a clean no-op. The semver maths lives in a
  shellcheck-clean, bats-tested helper `scripts/compute-release.sh`. The
  workflow is non-recursive and idempotent. (#5.2-001)
- Conventional Commits enforcement: a `.commitlintrc.json` extending
  `@commitlint/config-conventional` that restricts types to `feat`, `fix`,
  `chore`, `docs`, `refactor`, `test`, `ci`, `perf`, `build`, `revert`, makes
  scope optional but lower-case, and enforces a lower-case subject start, no
  trailing period, and a 72-character header cap. A new `commit-format` CI job
  in `.github/workflows/ci.yml` runs `commitlint --from origin/main --to HEAD`
  on every pull request. A "Commit Format" section in `CLAUDE.md` documents how
  to write a commit message with three concrete examples. (#5.1-001)
- Behavior test suites under `tests/`: `cmux-bridge.bats` extended with
  `notify` JSON-validity cases and graceful-degradation exit checks, and a new
  `install-dry-run.bats` that runs `./install.sh --dry-run` against an isolated
  `HOME`. A new `behavior-tests` CI job runs both suites. (#2.1-002)
- GitHub Actions workflow `.github/workflows/ci.yml` with a `static-checks`
  job: `shellcheck` on the project's shell scripts, `jq -e .` validation of
  plugin/MCP/settings JSON, and `markdown-link-check` on every tracked `*.md`
  file. (#2.1-001)
- Agent-registry validator `scripts/validate-agent-registry.sh` plus a
  `contract-checks` CI job that resolves every `subagent_type=` reference
  against the file basenames in `agents/`, so the `qa-expert` class of bug
  cannot recur. (#2.1-003)

### Fixed

- Corrected a broken table-of-contents anchor in `docs/claude-md-guide.md`
  ("Part IV") and consumed unused stdin in the `cmux-permission` hooks so the
  `static-checks` CI job passes on `main`. (#2.1-001)
- Reconciled slash-command references in `CLAUDE.md` to the bare-name form used
  in `README.md` and `WORKFLOW-v2.md`. Every command referenced in `CLAUDE.md`
  now resolves to an existing file. (#1.1-002)
- Aligned `qa-expert` references to `qa-engineer` across all SDLC skills, agent
  definitions, command files, and `CLAUDE.md` so QA coverage and E2E gates
  dispatch the defined specialist agent instead of silently falling back to
  `general-purpose`. (#1.1-001)
- Resolved dangling references to `WORKFLOW.md` and `workflow-diagram.png` in
  the foundation docs. (#1.2-001)
- Fixed the `.env` source path and a worktree leak in the cmux integration.
  (#1.3-002)
- Fixed Telegram JSON escaping in `cmux-bridge.sh`. (#1.3-001)

## [v1.3.0] - 2026-05-19

### Added

- Rate-limit segment in the statusline.
- `/create-story` skill that infers the epic from a requirement and gates
  too-large asks.
- MVP and roadmap epics seeded under `docs/stories/` for sharing the framework.

### Changed

- Enabled Codex auto permissions and documented Codex adversarial reviews.

## [v1.2.0] - 2026-05-19

### Added

- `autonomous-sdlc` plugin packaging the SDLC skills, usable both from Claude
  and from Codex.

### Changed

- Documented the dual `autonomous-sdlc` plugin (Claude + Codex) in `README.md`,
  split the plugin install instructions into GitHub-direct vs local-clone
  paths, and added a parallel build screenshot.
- Enabled the `autonomous-sdlc` plugin and push notifications.

## [v1.1.0] - 2026-05-19

### Added

- `project-init` and `brainstorm` skills, with seed-document support.
- Per-agent structured cmux log events for parallel `/build-stories`.
- `forge-worktree-bootstrap` hook for inheriting Bash permissions in worktrees.
- `--skip-tools` flag and CLI-tools installation in `install.sh`.
- Best-practice reference docs for Python, containers, testing, and databases.
- Deep-dive guide on `CLAUDE.md` structure, guardrails, and maintenance.
- Karpathy surgical-changes and verifiable-goals rules adopted in `CLAUDE.md`.
- `dev()` cmux workspace function in `install.sh`.

### Changed

- Made parallel worktree mode the default for `/build-stories`.
- Replaced workflow docs with diagram images and added a vision-doc-review
  skill.

### Fixed

- Cleared stale permission pills in the cmux sidebar on the `Stop` and
  `PreToolUse` hooks.

### Security

- Wrapped untrusted GitHub issue bodies in delimited input tags for sub-agent
  prompts.

## [v1.0.0] - 2026-05-19

### Added

- Initial public release of the Claude Code configuration framework: agents,
  skills, commands, hooks, the cmux integration, and the install script.
