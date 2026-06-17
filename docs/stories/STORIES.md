# USER STORIES: claude-code-config Sharing and Platform Roadmap

This is the master story index covering three progressive paths for the `claude-code-config` framework: foundation stabilization, MVP for team sharing (5 LTM colleagues on macOS and Windows-via-WSL2), and platform-grade roadmap items.

The MVP target is shareability: five LTM colleagues can install the framework on macOS or Windows-with-WSL2 in under 15 minutes, run `/brainstorm` then `/build-stories` on a fresh repo, and get working code, with cmux and Telegram both optional. Every commit to `main` produces a clean semver tag and an auto-generated GitHub Release. State survives crashes.

## User Personas

### Primary Personas

#### FX (Framework Owner)
- **Role**: Vice President Global Head of Sales, Banking Transformation, LTM. Builds and runs the framework on weekends.
- **Goals**: Ship a framework five LTM colleagues can install and use without help. Eventually evolve the framework into a reliable autonomous SDLC system that survives long-running unattended batches.
- **Pain Points**: Documentation drift between docs and code. Markdown-as-state losing data on long parallel runs. Manual release tracking. No tests on the framework itself. 613 MB of orphan worktrees from prior runs sitting on disk.

#### LTM Colleague (Early Adopter)
- **Role**: Sales engineer or solution architect inside LTM. Wants to automate dev side-projects with Claude Code, primarily as a productivity tool rather than a platform investment.
- **Goals**: Install in under 15 minutes. Run the autonomous build pipeline on a fresh repo and get a working PR. Optional dependencies (cmux, Telegram) never block the install or the first run.
- **Pain Points**: Mixed Mac and Windows fleet (WSL2 available on Windows boxes). Limited tolerance for failed installs or undocumented assumptions. No appetite to debug bash hooks.

### Secondary Personas

#### External Contributor (Post-MVP)
- **Role**: Advanced Claude Code user discovering the public repo after the LTM pilot.
- **Goals**: Read CHANGELOG, install cleanly, submit PRs that pass CI on first try.
- **Pain Points**: Undocumented assumptions about runtime, version drift between releases, no CI to validate contributions.

## Epic Overview

| Epic ID | Epic Name | Track | Story Count | Total Points | Priority | Status |
|---------|-----------|-------|-------------|--------------|----------|--------|
| Epic-01 | Stabilize Foundation | MVP-blocking | 5 | 13 | P0 | **COMPLETE** |
| Epic-02 | Self-CI for the Framework | MVP-blocking | 3 | 8 | P0 | **COMPLETE** |
| Epic-03 | Cross-Platform Installer (macOS + Windows/WSL2) | MVP | 4 | 13 | P0 | **COMPLETE** |
| Epic-04 | Durable State with SQLite | MVP | 4 | 18 | P1 | **COMPLETE** |
| Epic-05 | Automatic Release Management | MVP | 3 | 8 | P1 | **COMPLETE** |
| Epic-06 | Public Release Readiness | MVP | 4 | 9 | P1 | **COMPLETE**[^1] |
| Epic-07 | External Controller and Typed Contracts | Roadmap | 4 | 26 | P2 | **COMPLETE** (PRs #40-#43; E2E_PASS after bugfix #45) — `sdlc build`/`validate`/`sync-check` implemented; `init`/`resume`/`status`/`state`/`rollback` ship as [stubs](./epic-07-external-controller.md#deferred--stubbed-subcommands) |
| Epic-08 | Adversarial Gate and High-Risk Approval | Roadmap | 3 | 13 | P2 | **COMPLETE** (PRs #51, #52, #54) — adversarial slot + Codex reference impl + high-risk approval gate; gate approval fixed for solo/non-org repos via the `risk-approved` maintainer label ([#56](https://github.com/fxmartin/claude-code-config/pull/56)) |
| Epic-09 | Security Baked into Quality Gates + Live Pilot | Roadmap | 4 | 12 | P2 | **CODE-COMPLETE** (3/4) — SAST (#58), gitleaks (#59), osv-scanner (#60) merged, E2E_PASS; [9.3-001 live pilot](./epic-09-security-quality-gates.md#story-93-001-five-colleague-live-pilot) BLOCKED pending human pilot |
| Epic-10 | Controller Hardening (resume, observability, rollback) | Roadmap | 2 | 13 | P2 | **PLANNED** |

## Epic Navigation

- **[Epic-01: Stabilize Foundation](./epic-01-stabilize-foundation.md)** - Fix the bugs and drift surfaced by the multi-angle review (qa-expert vs qa-engineer, WORKFLOW.md, slash-name drift, Telegram JSON escape, worktree leak, `.env` source path).
- **[Epic-02: Self-CI for the Framework](./epic-02-self-ci.md)** - GitHub Actions: shellcheck, JSON schema validation, markdown link-check, bats tests, install dry-run smoke, agent-registry validator.
- **[Epic-03: Cross-Platform Installer](./epic-03-cross-platform-installer.md)** - Split the installer into modes. Document WSL2 path on Windows. Verify on clean machines of each platform.
- **[Epic-04: Durable State with SQLite](./epic-04-sqlite-state-ledger.md)** - Replace `.build-progress.md` as the truth source with SQLite. Keep markdown as the human-readable view.
- **[Epic-05: Automatic Release Management](./epic-05-release-management.md)** - Conventional Commits, commitlint on PRs, GitHub Actions semver bumper, auto-tag (`vX.Y.Z`), auto-generated GitHub Release notes, CHANGELOG maintenance.
- **[Epic-06: Public Release Readiness](./epic-06-public-release-readiness.md)** - CHANGELOG bootstrap, onboarding doc, five-user pilot smoke test, scope cleanup (separate personal agents from plugin).
- **[Epic-07: External Controller and Typed Contracts](./epic-07-external-controller.md)** *(Roadmap)*: Python or TypeScript CLI that owns the state machine; skills become workers with typed JSON-schema I/O contracts.
- **[Epic-08: Adversarial Gate and High-Risk Approval](./epic-08-adversarial-gate.md)** *(Roadmap)*: Vendor-agnostic adversarial reviewer slot; mandatory human approval for changes touching auth, payments, migrations, infrastructure, secrets.
- **[Epic-09: Security Baked into Quality Gates](./epic-09-security-quality-gates.md)** *(Roadmap)*: SAST plus dependency plus secrets scanning embedded into the coverage stage so security is a gate, not a follow-up. Closes with the five-colleague live pilot (9.3-001, moved from Epic-06 on 2026-06-11) — the roadmap capstone that validates the finished platform.
- **[Epic-10: Controller Hardening](./epic-10-controller-hardening.md)** *(Roadmap)*: Implement the `sdlc` CLI verbs Epic-07 shipped as stubs — `resume` (controller-native crash recovery), `status`/`state` (observability), `rollback` (checkpoint unwind) — and retire or implement the redundant `init`. Created 2026-06-15.

## MVP Summary

### MVP Criteria

The MVP is shippable when ALL of the following hold:

1. Five LTM colleagues can install the framework on macOS or Windows-with-WSL2 in under 15 minutes without contacting FX.
2. The autonomous build pipeline (`/build-stories`) runs end-to-end on a sample project, with cmux and Telegram both optional and never-blocking.
3. Every commit to `main` produces a clean semver tag (`vX.Y.Z`) and an auto-generated GitHub Release.
4. CI runs on every PR: shellcheck, JSON schema, markdown link-check, install dry-run, agent-registry validator.
5. State survives a mid-run crash: resuming a build picks up at the exact failed stage, with branch, PR number, and attempt count intact.
6. No reference in docs or skills points at a nonexistent file or agent.

### MVP Scope

| Track | Epics in MVP | Points |
|-------|--------------|--------|
| Foundation (must) | Epic-01, Epic-02 | 21 |
| Distribution (must) | Epic-03, Epic-06 | 22 |
| Durability (must) | Epic-04 | 18 |
| Release ops (must) | Epic-05 | 8 |
| **Total MVP** | **6 epics, 23 stories** | **69** |

### MVP Status

**6 of 6 MVP epics are COMPLETE.** All 23 MVP stories have landed. The framework is feature-complete and shippable to the five LTM colleagues.[^1]

[^1]: Epic-06 code-complete as of the batch build (PRs #33, #34, #35, #36 merged). The five-colleague live pilot originally gated this epic; on 2026-06-11 it was resequenced to the end of the roadmap as [Epic-09 Story 9.3-001](./epic-09-security-quality-gates.md#story-93-001-five-colleague-live-pilot), so it validates the finished platform (Epics 07–09) rather than the bare MVP. The MVP install/run criteria below remain verified by that pilot when it runs.

### Out of MVP Scope

- Native PowerShell support on Windows (WSL2 only for MVP).
- External controller (Epic-07).
- Mandatory adversarial gate (Epic-08; MVP keeps it optional and documented as a slot).
- Security scans baked into coverage stage (Epic-09).
- Linux desktop distros, ARM Linux servers, Bash 3 (macOS default bash).

## Project Metrics

- **Total Epics**: 10
- **Total Stories**: 36 *(the live pilot split out of 6.3-001 into 9.3-001 on 2026-06-11; Epic-10 added 2 controller-hardening stories on 2026-06-15)*
- **Total Story Points**: 133
- **MVP Stories**: 23 (69 pts)
- **Roadmap Stories**: 13 (64 pts)

## Story Dependencies

### Cross-Epic Dependencies

```mermaid
flowchart TD
    E01[Epic-01: Stabilize Foundation]
    E02[Epic-02: Self-CI]
    E03[Epic-03: Cross-Platform]
    E04[Epic-04: SQLite State]
    E05[Epic-05: Release Mgmt]
    E06[Epic-06: Public Release]
    E07[Epic-07: External Controller]
    E08[Epic-08: Adversarial Gate]
    E09[Epic-09: Security Gates]
    E10[Epic-10: Controller Hardening]

    E01 --> E02
    E01 --> E03
    E02 --> E05
    E04 --> E06
    E03 --> E06
    E05 --> E06
    E06 --> E07
    E04 --> E07
    E07 --> E08
    E07 --> E09
    E07 --> E10
```

### Critical Path

`Epic-01 → Epic-02 → Epic-05 → Epic-06 → Epic-07 → Epic-08 / Epic-09 → live pilot (9.3-001)`

Foundation fixes unblock CI. CI unblocks reliable release tagging. Release tagging unblocks public release readiness. Epic-03 (cross-platform installer) and Epic-04 (SQLite state) run in parallel to the critical path and converge at Epic-06. The five-colleague live pilot closes the roadmap as the final story of Epic-09 (resequenced 2026-06-11; it previously gated Epic-06).

### Recommended Sequencing

| Sprint | Focus | Epics in play |
|--------|-------|---------------|
| Sprint 1 | Foundation | Epic-01 in full, Epic-02 stories 2.1-001 and 2.1-002 |
| Sprint 2 | CI + Cross-platform | Epic-02 finish, Epic-03 in full |
| Sprint 3 | State + Release | Epic-04, Epic-05 |
| Sprint 4 | Public release | Epic-06 |
| Sprint 5+ | Roadmap | Epic-07, Epic-08, Epic-09 — closing with the five-colleague live pilot (9.3-001) |

The roadmap epics are not committed to a sprint. The five-colleague live pilot runs last (Epic-09 Story 9.3-001, resequenced 2026-06-11) and validates the finished platform.
