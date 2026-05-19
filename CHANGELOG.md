# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
