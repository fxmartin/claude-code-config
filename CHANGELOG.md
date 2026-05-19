# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

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
