# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v1.78.0] - 2026-06-25

### Added

- feat(agent-output-quality): wire a lightweight eval into (#18.1-003)


## [v1.77.0] - 2026-06-25

### Added

- feat(agent-output-quality): variant comparison and (#18.1-002)


## [v1.76.0] - 2026-06-25

### Added

- feat(agent-output-quality): reproducible agentic eval (#18.1-001)


## [v1.75.0] - 2026-06-25

### Added

- feat(agent-output-quality): keep user-facing docs current (#18.3-001)


## [v1.74.0] - 2026-06-25

### Added

- feat(agent-output-quality): over-engineering review lens (#18.2-001)


## [v1.73.2] - 2026-06-25

### Fixed

- fix(install): refuse --core install from an agent worktree


## [v1.73.1] - 2026-06-25

### Fixed

- fix(controller): lock worktrees so gc hook cannot reap mid-build


## [v1.73.0] - 2026-06-25

### Added

- feat(operability-self-service): `sdlc status --markdown` (#15.1-002)


## [v1.72.0] - 2026-06-25

### Added

- feat(operability-self-service): hook profiles and context (#15.2-001)


## [v1.71.0] - 2026-06-25

### Added

- feat(operability-self-service): `sdlc repair` — restore (#15.1-003)


## [v1.70.0] - 2026-06-25

### Added

- feat(operability-self-service): `sdlc doctor` health-check (#15.1-001)


## [v1.69.0] - 2026-06-25

### Added

- feat(operability-self-service): `sdlc clean` — safe (#15.3-001)


## [v1.68.0] - 2026-06-25

### Added

- feat(agent-runtime-security): scan (#13.2-001) (#171)


## [v1.67.0] - 2026-06-24

### Added

- feat(agent-runtime-security): optional container sandbox (#13.4-002)


## [v1.66.0] - 2026-06-24

### Added

- feat(agent-runtime-security): sanitize untrusted inputs (#13.3-001)

### Fixed

- fix(agent-runtime-security): sanitize untrusted inputs (#13.3-001)


## [v1.65.0] - 2026-06-24

### Added

- feat(agent-runtime-security): kill-switch and heartbeat (#13.4-001)


## [v1.64.0] - 2026-06-24

### Added

- feat(agent-runtime-security): deny-rules baseline for (#13.1-001)


## [v1.63.0] - 2026-06-23

### Added

- feat(parallel-execution): make `mode` authoritative and (#17.3-001)

### Fixed

- fix(parallel-execution): resume override widens serial run (#17.3-001)
- fix(parallel-execution): re-stamp worker cap on resume (#17.3-001)


## [v1.62.0] - 2026-06-23

### Added

- feat(parallel-execution): safe worktree integration and (#17.2-002)


## [v1.61.0] - 2026-06-23

### Added

- feat(parallel-execution): bounded concurrent execution of (#17.1-001)


## [v1.60.0] - 2026-06-23

### Added

- feat(parallel-execution): controller-owned git worktree (#17.2-001)


## [v1.59.0] - 2026-06-23

### Added

- feat(parallel-execution): concurrency-safe ledger writes (#17.1-002)


## [v1.58.2] - 2026-06-23

### Fixed

- fix(install): stop --core rewriting command symlinks as absolute (#156)


## [v1.58.1] - 2026-06-23

### Changed

- refactor(commands): relative symlinks into shared-skills (#155)


## [v1.58.0] - 2026-06-23

### Added

- feat(realtime-observability): stack each story into a (#11.2-014)


## [v1.57.3] - 2026-06-23

### Fixed

- fix(dashboard): keep view-session link on one line (#152)


## [v1.57.2] - 2026-06-23

### Changed

- refactor(hooks): remove cmux integration; extract notify-telegram (#143) (#151)


## [v1.57.1] - 2026-06-23

### Changed

- refactor(hooks): relocate worktree gc out of cmux-stop (#142) (#150)


## [v1.57.0] - 2026-06-23

### Added

- feat(controller): emit telegram notifications at run lifecycle events (#141) (#149)


## [v1.56.1] - 2026-06-23

### Fixed

- fix(release): bump controller version in lockstep with the tag (#148)


## [v1.56.0] - 2026-06-23

### Added

- feat(realtime-observability): show story titles on the dashboard (#147)


## [v1.55.0] - 2026-06-23

### Added

- feat(realtime-observability): stable-height live regions (#11.2-011) (#146)


## [v1.54.0] - 2026-06-23

### Added

- feat(realtime-observability): wave-column dependency dag in run detail (#140)


## [v1.53.0] - 2026-06-22

### Added

- feat(realtime-observability): github repo health on the dashboard

### Fixed

- fix(realtime-observability): refresh github stats on a steady cadence


## [v1.52.0] - 2026-06-22

### Added

- feat(realtime-observability): surface fix-issue runs (#11.2-013)


## [v1.51.0] - 2026-06-22

### Added

- feat(realtime-observability): live story status in dashboard (#11.2-009)


## [v1.50.0] - 2026-06-22

### Added

- feat(realtime-observability): persist story wave + deps (#11.2-007)
- feat(realtime-observability): central run registry (#11.2-001)


## [v1.49.10] - 2026-06-22

### Fixed

- fix(controller): render skipped-story pipeline cells as skipped (#131)


## [v1.49.9] - 2026-06-22

### Fixed

- fix(controller): apply stream resetsat to matched rate-limit signals (#127)


## [v1.49.8] - 2026-06-22

### Fixed

- fix(controller): surface stream resetsat into rate-limit reset_at signal (#126)


## [v1.49.7] - 2026-06-22

### Fixed

- fix(deps): audit project deps not pip-audit tool env (#119) (#125)


## [v1.49.6] - 2026-06-22

### Fixed

- fix(dashboard): render times in viewer local timezone (#77) (#124)


## [v1.49.5] - 2026-06-22

### Fixed

- fix(controller-robustness): refresh registry on resume finalize (#121) (#123)


## [v1.49.4] - 2026-06-21

### Fixed

- fix(controller-robustness): classify context-overflow distinctly (#104) (#118)


## [v1.49.3] - 2026-06-21

### Fixed

- fix(controller-robustness): backfill pr and stages on reconcile (#105) (#117)


## [v1.49.2] - 2026-06-21

### Fixed

- fix(controller-robustness): reject empty branch in reconcile (#111) (#116)


## [v1.49.1] - 2026-06-21

### Fixed

- fix(controller-robustness): detect 429 session-limit (#109) (#115)


## [v1.49.0] - 2026-06-21

### Added

- feat(cost-model-governance): thinking-token cap and early-compaction


## [v1.48.0] - 2026-06-21

### Added

- feat(cost-model-governance): cheap-first dispatch with retry escalation

### Fixed

- fix(cost-model-governance): preserve cheap-first escalation on resume


## [v1.47.0] - 2026-06-21

### Added

- feat(cost-model-governance): pre-dispatch cost estimate (#14.1-002)


## [v1.46.0] - 2026-06-21

### Added

- feat(cost-model-governance): per-task model routing (#14.2-001)

### Fixed

- fix(cost-model-governance): route bugfix/reask stages (#14.2-001)
- fix(cost-model-governance): resume-deterministic routing (#14.2-001)


## [v1.45.0] - 2026-06-21

### Added

- feat(cost-model-governance): per-run token budget gate (#14.1-001)

### Changed

- refactor(controller-robustness): normalize discovery format (#12.5-001)

### Fixed

- fix(cost-model-governance): re-enforce budget on resume (#14.1-001)


## [v1.44.0] - 2026-06-21

### Added

- feat(controller-robustness): share run-finalization helper (#12.3-004)


## [v1.43.0] - 2026-06-21

### Added

- feat(controller-robustness): parse intended dependency edges (#12.5-001)


## [v1.42.0] - 2026-06-21

### Added

- feat(controller-robustness): cut branches from origin/main (#12.4-001)


## [v1.41.0] - 2026-06-21

### Added

- feat(controller-robustness): add awaiting_approval run state (#12.3-003)


## [v1.40.0] - 2026-06-21

### Added

- feat(controller-robustness): add sdlc reconcile verb (#12.3-002)


## [v1.39.0] - 2026-06-21

### Added

- feat(controller-robustness): generate compliant commit subjects (#12.2-004) (#96)


## [v1.38.0] - 2026-06-21

### Added

- feat(controller-robustness): reconcile per-story status before terminal


## [v1.37.0] - 2026-06-21

### Added

- feat(controller-robustness): guard preflight recursion hangs (#12.1-002)


## [v1.36.0] - 2026-06-21

### Added

- feat(controller-robustness): auto-migrate ledger at launch (#12.2-003)

### Fixed

- fix(controller-robustness): add missing _migrations table (#12.2-003)


## [v1.35.0] - 2026-06-21

### Added

- feat(controller-robustness): lint agent commit messages (#12.2-002)
- feat(controller-robustness): make renderer non-destructive (#12.2-001)
- feat(controller-robustness): recover malformed envelope (#12.1-001)

### Fixed

- fix(controller-robustness): include commitlint rows in resume seq
- fix(controller-robustness): splice first matched marker pair (#12.2-001)
- fix(controller-robustness): keep render idempotent (#12.2-001)


## [v1.34.0] - 2026-06-20

### Added

- feat(realtime-observability): show run/story durations (#11.2-005)


## [v1.33.0] - 2026-06-20

### Added

- feat(realtime-observability): live per-run detail view (#11.2-004)
- feat(realtime-observability): live auto-refresh transport (#11.2-003)
- feat(realtime-observability): multi-run dashboard overview (#11.2-002)


## [v1.32.0] - 2026-06-20

### Added

- feat(realtime-observability): track running token usage (#11.1-003)

### Fixed

- fix(realtime-observability): don't write zero-token rows (#11.1-003)


## [v1.31.0] - 2026-06-20

### Added

- feat(realtime-observability): emit sub-stage progress events (#11.1-002)


## [v1.30.0] - 2026-06-20

### Added

- feat(realtime-observability): central run registry (#11.2-001)

### Fixed

- fix(realtime-observability): harden registry vs bad rows (#11.2-001)


## [v1.29.0] - 2026-06-20

### Added

- feat(realtime-observability): stream agent output with transcript tee

### Fixed

- fix(realtime-observability): prevent watchdog false-timeout race
- fix(realtime-observability): enforce wall-clock timeout on streamed run


## [v1.28.0] - 2026-06-20

### Added

- feat(controller): rollback verb and remove init stub (#10.2-001) (#71)


### Added

- feat(controller-hardening): implement rollback and resolve the init stub (#10.2-001)

### Removed

- The redundant `sdlc init` verb — `build` already creates the ledger on first use (#10.2-001)

## [v1.27.0] - 2026-06-20

### Added

- feat(controller): native resume, status, and state verbs (#10.1-001)

## [v1.26.0] - 2026-06-20

### Added

- feat(dashboard): per-stage pipeline, run config, and token/cost (#67)


## [v1.25.0] - 2026-06-19

### Added

- feat(controller): dashboard, status, tolerant parsing, version badge (#66)


## [v1.24.0] - 2026-06-17

### Added

- feat: dependency scan with osv-scanner (#9.1-002) (#60)


## [v1.23.0] - 2026-06-16

### Added

- feat(security): gitleaks secrets scan on every pr (#9.2-001) (#59)


### Added

- feat(security): dependency scan with osv-scanner (#9.1-002)
- feat(security): gitleaks secrets scan on every pr (#9.2-001)

## [v1.22.0] - 2026-06-16

### Added

- feat: semgrep sast inside the coverage stage (#9.1-001) (#58)

## [v1.21.0] - 2026-06-16

### Added

- feat: codex reference implementation of adversarial slot (#8.1-002) (#54)


## [v1.20.1] - 2026-06-16

### Fixed

- fix(risk-gate): make high-risk approval satisfiable on solo repos (#56)


## [v1.20.0] - 2026-06-15

### Added

- feat(commands): add workflow helper commands (#49)


## [v1.19.0] - 2026-06-15

### Added

- feat: high-risk file pattern detection and approval block (#8.2-001) (#52)


### Added

- feat(adversarial): codex reference implementation of the reviewer slot
  (#8.1-002). A new `scripts/codex-adversarial-review.sh` wrapper is the first
  concrete plug-in for the Story 8.1-001 reviewer slot: it fetches a PR via
  `gh`, runs a Codex review skill (`roast` or `project-review`) through
  `codex exec`, and emits JSON that validates against the
  `adversarial-reviewer-response` schema, so the controller's
  `parse_reviewer_response()` accepts it unchanged. The `codex` reviewer is
  registered and enabled by default in
  `controller/config/adversarial-reviewers.yaml`; users without Codex set
  `enabled: false` for no behavior change. Covered by
  `tests/codex-adversarial-review.bats` and
  `controller/tests/test_codex_adversarial_review.py` (both hermetic via a
  captured-transcript seam). Documented in `docs/adversarial-review.md`.
- feat(risk-gate): high-risk file pattern detection and human-approval block
  (#8.2-001). A new `risk-gate` GitHub workflow flags any PR touching high-risk
  paths (auth, payments, migrations, infra, secrets, destructive shell) with the
  `risk:high` label, comments the matched files, and fails until a
  `risk-approver` team member approves. Detection logic ships as
  `scripts/risk-gate-detect.sh` and `controller/src/sdlc/risk_gate.py`, driven by
  `controller/config/high-risk-patterns.yaml` with additive per-repo overrides
  via `.sdlc-risk-config.yaml`. The merge agent refuses to merge a `risk:high` PR
  without human approval and never bypasses with `gh pr merge --admin`. Documented
  in `docs/high-risk-gate.md`.

## [v1.17.1] - 2026-06-12

### Fixed

- fix(controller): epic-07 e2e gate fixes (#44) (#45)


### Fixed

- fix(controller): bundle the agent JSON schemas inside the `sdlc` package
  (`controller/src/sdlc/schemas/`) and resolve them via `importlib.resources`,
  so `sdlc validate` works under `uv tool install` (the schemas previously lived
  outside the package and broke once the source tree was gone — Epic-07 E2E
  defect). Added a packaging regression test and a CI `sdlc validate` round-trip
  after install (#44).
- fix(agents): move `agents/contracts.md` to `docs/contracts.md`; the file is
  I/O contract documentation, not an agent definition, and its presence under
  `agents/` broke the "agents/ root contains exactly 8 plugin agents" invariant
  (#44).

## [v1.17.0] - 2026-06-12

### Added

- feat(controller): codex mirror sync mechanism (#7.4-001) (#43)

### Shared skills

Codex mirror artifact: bump the `shared-skills` submodule to this tag and run `git submodule update --remote` (see ADR-002).

- feat(controller): codex mirror sync mechanism (#7.4-001) (#43)


### Changed

- Established the Codex mirror sync mechanism (Epic-07, Story 7.4-001). The seven
  shared skills (`check-releases`, `coverage`, `create-issue`,
  `create-project-summary-stats`, `plan-release-update`, `project-review`,
  `roast`) now live in a single source of truth at `shared-skills/`; their
  duplicate copies under `commands/` are removed. The `nix-install` Codex mirror
  consumes them as a git submodule (ADR-002), updating with
  `git submodule update --remote` and verifying parity via `sdlc sync-check`
  (`scripts/sync-shared-skills.sh`). Each release tag is the versioned shared-skills
  artifact.

## [v1.16.0] - 2026-06-12

### Added

- feat(controller): port build-stories orchestration (#7.3-001) (#42)


### Changed

- Ported the `build-stories` orchestration out of the Claude skill into the
  external `sdlc` controller (Epic-07, Story 7.3-001). The controller now owns
  the deterministic state machine — preflight, story discovery from the
  markdown epics, dependency-cohort scheduling, the build → coverage → review →
  merge pipeline, and a bounded bugfix loop — in Python (`controller/src/sdlc/`:
  `build.py`, `cohort.py`, `dispatch.py`, `discovery.py`, `ledger_view.py`).
  Every agent is dispatched as a subprocess and its response is validated
  against the 7.2-001 JSON-schema contracts *before* the next stage runs; a
  malformed or schema-invalid response is treated as a build failure and routed
  to the bugfix loop. Stage transitions are persisted to the Epic-04 SQLite
  ledger after every step. `sdlc build [scope]` accepts the same flags the
  skill did (`--dry-run`, `--auto`, `--skip-coverage`, `--limit=N`,
  `--sequential`, `--coverage-threshold=N`, `--skip-preflight`).

  **Migration note:** `build-stories/SKILL.md` is now a thin wrapper that shells
  out to `sdlc build $ARGUMENTS` (falling back to `uv run sdlc` from the
  `controller/` checkout when the tool is not installed). Users still invoke
  `/build-stories` exactly as before — the change is invisible at the call site.
  Architecture is documented in `docs/controller-architecture.md`.

## [v1.15.0] - 2026-06-12

### Added

- feat(controller): define agent i/o json-schema contracts (#7.2-001) (#41)


### Added

- Typed agent I/O JSON-schema contracts (Epic-07, Story 7.2-001). Five JSON
  Schema draft 2020-12 schemas in `controller/schemas/` for the `build`,
  `coverage`, `review`, `merge`, and `bugfix` agent responses. Agents now emit
  their structured result as the final line of their response, fenced with
  `<<<RESULT_JSON>>>` ... `<<<END_RESULT>>>` markers. A new `sdlc.contracts`
  module parses the marker block and validates it (`jsonschema`), surfacing
  validation errors as actionable messages that name the offending field. The
  `sdlc validate <agent-type> [file]` command exposes this on the CLI (reads a
  file or stdin). Build-stories agent prompts updated to require the result
  block. New `docs/contracts.md` documents the contract; test harness
  `controller/tests/test_schemas.py` covers valid-passes, missing-required-fails
  -with-field-name, and extra-field-allowed (forward-compat).

## [v1.14.0] - 2026-06-12

### Added

- feat(controller): choose runtime and scaffold the cli (#7.1-001) (#40)


### Added

- External controller scaffold (Epic-07, Story 7.1-001). New `controller/`
  Python package managed with uv, exposing the `sdlc` CLI with `--version`,
  `--help`, an `init` stub, and stub subcommands for the full planned surface
  (`build`, `resume`, `status`, `state`, `validate`, `rollback`). The runtime
  decision (Python + uv + Typer + Pydantic) is recorded in
  `docs/adr/001-controller-runtime.md`. Installable via `uv tool install .`
  from `controller/`, or via the new `scripts/install-controller.sh` wrapper
  which bootstraps uv first for users who do not have it. A new CI
  `controller-smoke` job installs the CLI and asserts `sdlc --version` matches
  `controller/pyproject.toml` on macOS and Ubuntu, and runs the controller
  pytest suite (`controller/tests/test_cli.py`, 7 tests).

## [v1.13.2] - 2026-06-11

### Changed

- refactor(tests): fold test/ shell checkers into tests/

### Fixed

- fix(install): initialize git submodules during --core install
- fix(templates): use a valid model id in frontmatter examples


### Added

- Pilot kit for the five-LTM-colleague smoke test (#6.3-001). New
  `docs/pilot-kit/` houses four colleague-facing artifacts plus an
  environment-capture helper: `README.md` (one-page "what's expected of you"
  brief — onboarding read, one `/build-stories` run, one feedback form),
  `feedback-template.md` (blank structured form covering install time,
  blockers, what worked / didn't, 1–5 recommendation score, project
  details), `pilot-tracker.md` (FX's per-colleague ledger with platform /
  install path / gate dates / verdict / issue count columns), and
  `decision-record.md` (post-pilot pass/fail checklist tied to the epic-06
  acceptance criteria, must-fix vs deferred lists, and a three-option
  go/no-go decision block — all fields blank until the pilot actually
  runs). New `scripts/pilot-helper.sh` prints a paste-ready markdown
  Environment block (OS, architecture, shell, Claude Code / `gh` / `git`
  versions, install path) for the feedback form, with
  `PILOT_HELPER_NONINTERACTIVE=1` for CI / scripted use. README links to
  the pilot kit from the top of the Install section. New
  `tests/pilot-kit.bats` (7 assertions) pins the kit's structure and the
  helper's output contract. The actual five-colleague pilot remains
  pending — this delivers the kit, not the results.
- LTM colleague onboarding guide (#6.1-001). New `docs/onboarding.md` walks a
  new colleague from "I heard about this" to "I just ran `/build-stories` on a
  fresh project" in under 15 minutes. Sections cover prerequisites, both
  install paths (marketplace + `install.sh --core`/`--tools`/`--mcp`/`--shell`
  /`--all`), a first-run smoke test, the full `/brainstorm → /generate-epics
  → /create-epic → /create-story → /build-stories` walkthrough with expected
  events on success and failure modes on failure, optional cmux and Telegram
  integrations (both opt-in; cmux is macOS-only), Conventional Commits
  conventions, the SQLite state ledger and `/build-stories resume`, getting
  help, known limitations, and a "Tested with" footer dated 2026-05-20.
  README links to it from the top of the Install section. New
  `tests/onboarding-doc.bats` pins the cross-references. Colleague review
  remains pending until the Story 6.3-001 pilot kicks off.

## [v1.13.1] - 2026-05-20

### Changed

- refactor(plugin): separate personal config from autonomous-sdlc plugin (#6.2-001) (#33)


## [v1.13.0] - 2026-05-20

### Added

- feat(installer): verify both plugin install paths end-to-end (#6.4-001) (#34)
- Plugin install path verification (#6.4-001). New
  `scripts/verify-plugin-install.sh` validates the `/plugin marketplace add
  fxmartin/claude-code-config` path B structurally: marketplace manifest is
  valid JSON, every declared plugin resolves to a real directory with a
  valid `plugin.json`, and every `skills/<name>/SKILL.md` frontmatter `name:`
  matches its directory. Wired into `.github/workflows/ci.yml` static-checks
  alongside the existing path-A `scripts/smoke-test.sh`. New bats suite
  `tests/plugin-install-paths.bats` pins the contract. `docs/smoke-test.md`
  grows a "Two install paths" section listing the exact manual steps for the
  parts CI cannot reach (real `/plugin install` inside a Claude Code session
  on macOS and WSL2).

### Changed

- Separated personal config from the autonomous-sdlc plugin (#6.2-001).
  The four personal-helper agents — `crypto-coin-analyzer`,
  `crypto-market-agent`, `executive-summary-generator`, and
  `professional-profile-researcher` — moved from `agents/` to
  `agents/personal/`. The plugin-scope agents stay directly under
  `agents/` so an LTM colleague installing `autonomous-sdlc` sees only
  SDLC-relevant agents in their roster. The agent-registry validator
  (`scripts/validate-agent-registry.sh`) now walks `agents/`
  recursively so references in either location continue to resolve;
  every existing `subagent_type=` call still validates. The README
  agent roster splits into two tables (SDLC plugin agents vs Personal
  extras) so the boundary is documented for both colleagues and the
  forthcoming `docs/onboarding.md` (Story 6.1-001).
## [v1.12.0] - 2026-05-20

### Added

- feat(state): resume run from ledger state (#4.3-001) (#32)


### Added

- Resume build-stories runs from SQLite ledger state (#4.3-001).
  `scripts/sdlc-state.sh` grows three resume subcommands —
  `latest-incomplete-run`, `mark-stages-stale <run> <story> <stage>`,
  and `resume-plan <run>` — that let `/build-stories resume` rebuild
  the in-flight queue directly from SQLite. Branch names and PR numbers
  are preserved verbatim across the resume so the merge agent reuses the
  existing PR instead of creating a new one. `resume-plan` skips DONE
  stories, re-evaluates BLOCKED stories against the recorded dependency
  events (a BLOCKED story flips to PENDING once every dependency is DONE),
  surfaces FAILED / SKIPPED entries as-is for the orchestrator's
  `--auto` path, and emits a JSON envelope compatible with the discovery
  agent's `QUEUE_JSON:` contract. The discovery-agent prompt now drives
  Phase 3 resume through the ledger; the markdown progress file is the
  fallback path only when no ledger is configured. The merge-update
  prompt gained a resume-aware preflight that checks the PR still exists
  and exits `MERGE_STATUS: PR_MISSING` if the PR has been closed or
  deleted since the prior attempt.

## [v1.11.0] - 2026-05-20

### Added

- feat(state): generate .build-progress.md from sqlite ledger (#4.2-002) (#31)


### Added

- Windows install guide (WSL2-based) (#3.2-001)
- Markdown view generator: regenerate .build-progress.md from SQLite ledger (#4.2-002)
## [v1.9.0] - 2026-05-20

### Added

- feat(state): orchestrator and agents write to the ledger (#4.2-001) (#28)


### Added

- Orchestrator and build-stories agents now write run/story/stage/event
  records to the SQLite ledger (#4.2-001). `scripts/sdlc-state.sh` grows a
  write-path API (`run-create`, `run-update-status`, `story-upsert`,
  `stage-start`, `stage-finish`, `event-log`) with single-quote-doubled
  TEXT parameters and integer coercion for numeric IDs — no raw user input
  reaches SQL. A new `hooks/sdlc-state-emit.sh` wrapper is the single
  ingress for agents: it resolves the ledger DB from `$SDLC_STATE_DB`
  (set by the orchestrator) or the repo-root `.sdlc-state.db`, and
  silently no-ops when no ledger is configured so legacy environments
  are not broken. The `build-stories` skill, the parallel/sequential
  build prompts, the coverage-gate prompt, the review prompt, the
  merge-update prompt, and the E2E-gate path all emit ledger updates
  alongside their existing `cmux-bridge log` calls. The markdown
  progress file (`.build-progress.md`) remains the human-readable view;
  story 4.2-002 will switch it to a SELECT-only renderer over this
  ledger.

## [v1.8.0] - 2026-05-20

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
