# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Automatic release pipeline: a new `.github/workflows/release.yml` that, on
  every push to `main`, computes the next semantic version from the
  Conventional Commits since the last tag, aligns the `version` fields of
  `plugins/autonomous-sdlc/.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json`, prepends a CHANGELOG section, commits the
  bump as `chore(release): vX.Y.Z`, tags it `vX.Y.Z`, and publishes a GitHub
  Release with auto-generated notes. Bump rules: `BREAKING CHANGE:`/`!` →
  MAJOR, `feat` → MINOR, `fix`/`perf`/`refactor` → PATCH, and a
  chore/docs-only push is a clean no-op. The semver maths lives in a
  shellcheck-clean, bats-tested helper `scripts/compute-release.sh`. The
  workflow is non-recursive (a `guard` job skips its own `chore(release):`
  bump commit and `[skip release]` pushes) and idempotent (it no-ops if the
  computed tag already exists). (#5.2-001)
- Conventional Commits enforcement: a `.commitlintrc.json` extending
  `@commitlint/config-conventional` that restricts types to `feat`, `fix`,
  `chore`, `docs`, `refactor`, `test`, `ci`, `perf`, `build`, `revert`, makes
  scope optional but lower-case, and enforces a lower-case subject start, no
  trailing period, and a 72-character header cap. A new `commit-format` CI job
  in `.github/workflows/ci.yml` runs `commitlint --from origin/main --to HEAD`
  on every pull request and fails the PR on any non-conforming commit; existing
  history on `main` is exempt. A "Commit Format" section in `CLAUDE.md`
  documents how to write a commit message with three concrete examples.
  Folding the same guidance into `docs/onboarding.md` is deferred until Epic-06
  creates that file. (#5.1-001)
- Behavior test suites under `tests/`: `cmux-bridge.bats` extended with
  `notify` JSON-validity cases (normal and adversarial input) and
  graceful-degradation exit checks for the `log`, `status`, `progress`,
  `clear` and tokenless `telegram` subcommands; and a new
  `install-dry-run.bats` that runs `./install.sh --dry-run --skip-tools
  --skip-mcp` against an isolated `HOME`, asserting exit 0, no symlinks
  created (before/after snapshot), and a `[dry-run]` line for every target
  file. A new `behavior-tests` CI job in `.github/workflows/ci.yml` runs both
  suites via `bats-core/bats-action`. (#2.1-002)
- GitHub Actions workflow `.github/workflows/ci.yml` with a `static-checks`
  job: `shellcheck` (severity floor `warning`) on the project's shell scripts,
  `jq -e .` validation of plugin/MCP/settings JSON, and `markdown-link-check`
  on every tracked `*.md` file. Runs on `pull_request` and on `push` to
  `main`. The workflow declares least-privilege `permissions: contents: read`
  since the checks are read-only. Includes a `.markdown-link-check.json`
  config that allows `mailto:` links and allowlists known-transient hosts.
  Branch protection requiring the
  `static-checks` status check is an admin action for FX; it will be
  documented in `docs/onboarding.md` once Epic-06 creates that file. (#2.1-001)
- Agent-registry validator `scripts/validate-agent-registry.sh` plus a
  `contract-checks` CI job that runs it. The validator greps every `*.md`
  under `plugins/`, `skills/`, and `commands/` for `subagent_type=`
  references and resolves each against the file basenames in `agents/`,
  allowlisting built-in Claude Code subagent types (`general-purpose`,
  `Plan`, `Explore`) and skipping bracketed placeholders (e.g.
  `[story.agent_type]`). It exits non-zero listing every unresolved
  reference with its file and line, so the `qa-expert` class of bug cannot
  recur. The `static-checks` shellcheck glob now also covers `scripts/*.sh`.
  Documenting how to add a new agent so it is discoverable to the validator
  is deferred to `docs/onboarding.md` (an Epic-06 deliverable). (#2.1-003)

### Fixed

- Corrected a broken table-of-contents anchor in `docs/claude-md-guide.md`
  ("Part IV") and consumed unused stdin in the `cmux-permission` hooks via
  `cat > /dev/null` so the new `static-checks` CI job passes on `main`.
  (#2.1-001)

- Reconciled slash-command references in `CLAUDE.md` to the bare-name form used
  in `README.md` and `WORKFLOW-v2.md`: `/issues:create-issue` → `/create-issue`,
  `/quality:coverage` → `/coverage`,
  `/project:create-project-summary-stats` → `/create-project-summary-stats`.
  Every command referenced in `CLAUDE.md` now resolves to an existing file.
  (#1.1-002)
- Align `qa-expert` references to `qa-engineer` across all SDLC skills, agent
  definitions, command files, and `CLAUDE.md` so QA coverage and E2E gates
  dispatch the defined specialist agent instead of silently falling back to
  `general-purpose` (#1.1-001).
