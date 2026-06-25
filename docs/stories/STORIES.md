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
| Epic-10 | Controller Hardening (resume, observability, rollback) | Roadmap | 2 | 13 | P2 | **COMPLETE** (2/2) — 10.1-001 `resume`/`status`/`state` + a web `dashboard` (#66, #67, #70); 10.2-001 `rollback` + `init` removal (#71). All `sdlc` verbs implemented; no stubs remain |
| Epic-11 | Realtime Progress & Multi-Run Observability | Roadmap | 17 | 59 | P2 | **COMPLETE (17/17)** — all stories merged on `main` (PRs #78-#83 + dashboard batch through #154): realtime sub-stage/token streaming, multi-run registry + auto-refreshing dashboard, GitHub repo-health panel, wave-column dependency DAG (#140), live story status, transcript viewer, stable-height live regions, story titles, fix-issue runs |
| Epic-12 | Controller Robustness & Failure Recovery | Roadmap | 12 | 46 | P2 | **COMPLETE (12/12)** ([#72](https://github.com/fxmartin/claude-code-config/issues/72); PRs through #98) — all 12 stories merged on `main`: recover malformed result envelopes before parking, guard preflight against recursive hangs, non-destructive progress renderer, commit-message linting at commit time, auto-apply pending ledger migrations at launch; Feature 12.3/12.4 — honest run-terminal status (reconcile vs `origin/main`, `sdlc reconcile` verb, `AWAITING_APPROVAL` state, shared finalize helper) and branch-isolation (cut branches from `origin/main`); 12.5-001 dependency-line parser hardening; 12.2-004 compliant-commit-subjects-by-construction |
| Epic-13 | Agent Runtime Security Hardening | Roadmap | 5 | 19 | P2 | **COMPLETE (5/5)** (PRs #167-#171) — all stories merged on `main`: deny-rules for secret paths (#168), supply-chain scan of hooks/skills/MCP/settings (#171), untrusted-content sanitization (#167), kill-switch + heartbeat (#169), optional container sandbox (#170). Built in parallel (run 4fed56b0); 13.2-001 salvaged after a mid-run cmux-shim dispatch failure. From ECC `the-security-guide.md`. Created 2026-06-20 |
| Epic-14 | Cost & Model Governance | Roadmap | 6 | 23 | P2 | **COMPLETE (6/6)** (PRs through #114) — all stories merged on `main`: token-first budget gate, Max rate-limit/quota awareness with auto-resume on window reset, pre-dispatch usage estimate, per-task model routing (Balanced map: Haiku merge/discovery · Sonnet build/coverage/review · Opus high-risk + adversarial), cheap-first escalation on retry (#113), thinking-token cap (#114). From ECC `the-longform-guide.md`. Created 2026-06-20 |
| Epic-15 | Operability & Self-Service | Roadmap | 5 | 12 | P2 | **PLANNED** — new read-side verbs so colleagues self-diagnose: `sdlc doctor` health-check, `status --markdown` handoff, optional `repair`, hook profiles, and `sdlc clean` workspace GC (orphan worktrees / merged branches / stale logs, registry-safe). From ECC operator tooling + the worktree-cleanup session. Created 2026-06-20 |
| Epic-16 | Continuous Learning / "Instincts" | Roadmap | 2 | 8 | P3 | **PLANNED (EXPERIMENTAL)** — spike: mine completed runs (ledger + git) for confidence-scored learnings; human-gated promotion to a skill/doc draft (never auto-installed). From ECC "instincts". Created 2026-06-20 |
| Epic-17 | True Parallel Story Execution | Roadmap | 5 | 19 | P2 | **COMPLETE (5/5)** (PRs #161-#165) — all stories merged on `main`: bounded concurrent executor (default 5, `--concurrency=N`), per-story git-worktree isolation, concurrency-safe ledger writes, truthful concurrency in status, and `mode` made authoritative. Validated end-to-end by the epic-13 parallel run (4 concurrent agents). From the epic-11 run post-mortem. Created 2026-06-20 |
| Epic-18 | Agent Output Quality — Evaluation & Simplicity | Roadmap | 5 | 21 | P2 | **PLANNED** — fills the eval-harness gap: a reproducible agentic eval (LOC/tokens/cost/quality) with variant A/B + regression baselines + CI hook, an over-engineering review lens that flags over-built code on each story's diff, plus documentation-currency (18.3-001) — update user-facing docs for behavior-changing stories in-commit and flag stale docs at review (advisory; CHANGELOG stays with Epic-05). From the ponytail analysis (philosophy already in CLAUDE.md). Created 2026-06-20 |

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
- **[Epic-11: Realtime Progress & Multi-Run Observability](./epic-11-realtime-observability.md)** *(Roadmap)*: Stream agent stdout and emit fine-grained sub-stage events + running token/cost into the ledger (no more "captured output, invisible until stage completes"); a central run registry lets one auto-refreshing dashboard display every active run across repos. Created 2026-06-20.
- **[Epic-12: Controller Robustness & Failure Recovery](./epic-12-controller-robustness.md)** *(Roadmap)*: Make the controller resilient when a run goes sideways — recover a missing/malformed result envelope before parking work `NEEDS_ATTENTION`, guard preflight against recursive self-invocation hangs, keep the `.build-progress.md` renderer from clobbering non-ledger history, and lint agent commit messages at commit time. From issue #72 (epic-10 run post-mortem). Created 2026-06-20. Extended 2026-06-21 with Features 12.3/12.4 (honest run-terminal status that reconciles against `origin/main` so runs report DONE when work landed, an `AWAITING_APPROVAL` state for high-risk-gated merges, and branch-isolation to stop transitive landings) from the epic-11/12 mislabel post-mortem.
- **[Epic-13: Agent Runtime Security Hardening](./epic-13-agent-runtime-security.md)** *(Roadmap)*: Harden the *agent harness* (not the target code Epic-09 scans): a deny baseline for secret paths under `--dangerously-skip-permissions`, supply-chain scanning of hooks/skills/MCP/settings, untrusted-content sanitization before dispatch, a kill-switch + heartbeat for runaway agents, and an optional no-egress container sandbox. Inspired by ECC `the-security-guide.md`. Created 2026-06-20.
- **[Epic-14: Cost & Model Governance](./epic-14-cost-model-governance.md)** *(Roadmap)*: Make cost *enforceable* (per-run budget gate that pauses/aborts resumably; pre-dispatch estimate) and route models by task complexity/risk (Haiku/Sonnet/Opus), plus a thinking-token cap. Consumes Epic-11's token/cost accrual. Inspired by ECC `the-longform-guide.md`. Created 2026-06-20.
- **[Epic-15: Operability & Self-Service](./epic-15-operability-self-service.md)** *(Roadmap)*: New read-side verbs so the five colleagues self-diagnose instead of pinging FX — `sdlc doctor` (install/ledger/stuck-run/config/dependency health), `status --markdown` portable handoff, optional `sdlc repair`, and hook profiles. New verbs, so it lives here rather than Epic-12. Inspired by ECC operator tooling. Created 2026-06-20.
- **[Epic-16: Continuous Learning / "Instincts"](./epic-16-continuous-learning.md)** *(Roadmap, EXPERIMENTAL)*: A spike to mine completed runs (ledger + git history) for confidence-scored recurring patterns, then human-gated promotion of high-confidence learnings into reviewable skill/doc drafts — never auto-installed. Lowest priority; gated on the spike's go/no-go. Inspired by ECC "instincts". Created 2026-06-20.
- **[Epic-17: True Parallel Story Execution](./epic-17-parallel-execution.md)** *(Roadmap)*: The scheduler computes dependency cohorts but the executor runs stories one at a time — `mode=parallel` is a label nothing reads. Add a bounded concurrent executor (cohort-barrier, default 5 workers, `--concurrency=N`), per-story git-worktree isolation, concurrency-safe ledger writes, and truthful multi-active status. `--sequential` preserves today's serial path. From the epic-11 run post-mortem. Created 2026-06-20.
- **[Epic-18: Agent Output Quality — Evaluation & Simplicity](./epic-18-agent-output-quality.md)** *(Roadmap)*: Two capabilities we lack — a reproducible **agentic eval harness** (score agent output on LOC/tokens/cost/quality across real tickets; variant A/B; regression baselines; CI hook) to measure prompt/model/skill changes (incl. validating Epic-14 routing), and an **over-engineering review lens** that flags over-built code on each story's diff (advisory or route-to-simplify) — operationalizing the `CLAUDE.md` complexity-check where no human is in the loop. From the ponytail analysis; its philosophy was already ours. Created 2026-06-20.

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

- **Total Epics**: 18
- **Total Stories**: 93 *(the live pilot split out of 6.3-001 into 9.3-001 on 2026-06-11; Epic-10 added 2 controller-hardening stories on 2026-06-15; Epic-11 added 7 realtime/multi-run stories on 2026-06-20, +1 duration story 11.2-005, +1 GitHub repo-health story 11.2-006, +2 wave-visualization stories 11.2-007/008, +1 live story-status story 11.2-009, +1 transcript-viewer story 11.2-010; Epic-12 added 4 controller-robustness stories on 2026-06-20 from issue #72, +1 ledger auto-migrate story 12.2-003; Epics 13–16 added 15 stories on 2026-06-20 from the ECC feature analysis — security hardening, cost/model governance, operability, and an experimental continuous-learning spike; Epic-17 added 5 parallel-execution stories on 2026-06-20 from the epic-11 run post-mortem; Epic-15 +1 `sdlc clean` workspace-GC story 15.3-001 on 2026-06-20; Epic-14 +1 Max rate-limit/quota story 14.1-003 on 2026-06-20 — token-first revision for the Max subscription billing model, with auto-resume on window reset; Epic-18 added 4 agent-output-quality stories on 2026-06-20 from the ponytail analysis — eval/benchmark harness + over-engineering review lens; Epic-14 +1 cheap-first model-escalation story 14.2-003 on 2026-06-21 — concrete Balanced per-task model map; Epic-12 +5 Feature 12.3/12.4 stories on 2026-06-21 from the epic-11/12 mislabel post-mortem — honest run-terminal status (reconcile vs `origin/main` 12.3-001, `sdlc reconcile` 12.3-002, `AWAITING_APPROVAL` state 12.3-003, shared finalize helper 12.3-004) and branch-isolation 12.4-001; Epic-12 +1 dependency-line parser story 12.5-001 on 2026-06-21 — discovery must parse only intended edges, not prose-mentioned IDs, root-cause fix behind PR #92; Epic-12 +1 compliant-commit-subjects story 12.2-004 on 2026-06-21 — make agent commit subjects commitlint-compliant by construction, root-cause fix for the run 7df64f19 commitlint stall; Epic-11 +1 stable-height live-regions story 11.2-011 on 2026-06-21 — fix dashboard screen-jump on live update, and revised 11.2-006 for the multi-run dashboard; corrected the Total Stories headline from a stale 81 left by the Epic-12 build; Epic-11 +2 dashboard stories on 2026-06-22 — story titles on the dashboard 11.2-012 and surfacing fix-issue runs in the dashboard 11.2-013; Epic-18 +1 documentation-currency story 18.3-001 on 2026-06-23 — fold doc updates into the build stage's DoD + a review-stage staleness lens, closing the gap that the 4-stage pipeline never updates user-facing docs; corrected Epic-11 index count 16→17 / 56→59 pts on 2026-06-25 to match its epic-file overview and the ledger — 11.2-014 was never counted in the index; marked Epics 11/12/13/14/17 COMPLETE on 2026-06-25 to match merged work — Epic-13 built in parallel run 4fed56b0 (PRs #167-#171))*
- **Total Story Points**: 340
- **MVP Stories**: 23 (69 pts)
- **Roadmap Stories**: 70 (271 pts)

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
