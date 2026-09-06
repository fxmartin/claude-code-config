# REVIEW.md — Technical & Business Assessment

**Repository:** `fxmartin/claude-code-config` (public) · **Reviewed:** 2026-09-06 · **Scope:** the `sdlc` autonomous-SDLC controller, its installer, hooks, CI, and docs
**Method:** four independent senior-reviewer passes (architecture · security · performance/reliability · testing/business risk), each seeded with defects verified in live operation on 2026-09-05/06 and instructed to confirm against source with `file:line` evidence. Items marked **UNVERIFIED** could not be confirmed and are flagged, not dropped.

---

## Executive Summary (30 seconds)

**Health: strong engineering discipline on a fragile core.** 115K LOC, 4,138 passing tests at a measured **99% line coverage**, a ten-job CI pipeline including secrets and supply-chain scanning, and a working autonomous pipeline that shipped 11 stories across two epics in one day for $58 of tokens. Against that, the core state machine lives in a single **9,297-line / complexity-614 file**, the repo's flagship security control (the risk gate) **can be edited by the PR it is scanning**, and three separate failure classes observed this session (ledger migration, installer symlink hijack, silent one-hour hangs) all trace to guards that are heuristics rather than checks.

**Verdict: GO for its stated audience (solo maintainer, small trusted pilot). NO-GO for external contributors or a multi-maintainer team until the three P0 security items land** — the risk-gate trust loop, the harness privilege ceiling, and the installer's root trust model. Estimated total for all P0s: **~8–12 engineer-days**.

**Primary risks, in order:** (1) risk gate bypassable by the PR under review; (2) non-Claude harnesses run with no secret/egress deny floor and a merge agent that can `--admin`-merge on convention alone; (3) `build.py` god-module concentrates every recent defect and all bus-factor risk; (4) captured dispatch path can burn a silent hour per stage; (5) single maintainer with 67% agent-co-authored commits and an LLM as the only reviewer.

| Dimension | Score | One line |
|---|---|---|
| Architecture | ⚠️ C | Sound registry/ledger *ideas*, executed inside one 9.3K-line module |
| Security | 🔴 D+ | Good controls for the Claude harness; gate and non-Claude paths bypassable |
| Performance & reliability | ⚠️ C+ | Real spend is tracked well; hang/OOM/retry costs are not bounded |
| Code quality & tests | ✅ B+ | 99% coverage, mature CI — but coverage isn't gated and tests are brittle |
| Business readiness | ⚠️ C | Pilot-ready; bus factor 1; docs drift within hours |

---

## Critical Issues (P0)

### SEC-1 · The risk gate evaluates policy and detector code from the untrusted PR checkout — 🔴 Critical
`.github/workflows/risk-gate.yml:45` checks out the PR ref; `:58` runs `bash scripts/risk-gate-detect.sh` from that checkout against `controller/src/sdlc/config/high-risk-patterns.yaml` from the same checkout. **Verified:** the policy file is not matched by its own patterns.
```
controller/src/sdlc/config/high-risk-patterns.yaml  → NOT matched
.github/workflows/ci.yml                            → MATCHED
```
**Scenario:** one PR narrows the patterns and smuggles a workflow/shell change in the same diff; the gate reports green; the human-approval step never fires. The controller-side `risk_gate.py:20-23` is immune (loads from the installed package) — the CI job is the one that gates merges. **Fix (M, 3–5d):** evaluate detector + policy from the base branch (or a pinned ref), and add the policy file and detector script to the protected set. **Owner: FX.**

### SEC-2 · Non-Claude harnesses get no deny baseline; merge-agent restraint is a prompt, not a control — 🔴 High
`DENY_BASELINE` (`dispatch.py:88-95` — blocks `Read(~/.ssh/**)`, `Read(**/.env*)`, `curl|bash`, `ssh`) is appended only in `resolve_agent_cmd()` (`dispatch.py:150-186`), the built-in Claude path. `HarnessConfig.to_argv()` (`harness.py:163-181`) for registry harnesses (codex/qwen/opencode, all `enabled: true`) renders **no deny rules**. Separately, "never `gh pr merge --admin`" exists only as prompt text (`fix_issue.py:498-503`); nothing in the deny list blocks `Bash(gh pr merge --admin*)`, and the agent runs on the operator's admin-scoped `gh` auth under `--dangerously-skip-permissions`. **Scenario:** a prompt-injected issue body (the vector `sanitize.py` exists to blunt) or a non-compliant model run bypasses the risk gate with one command. **Fix (S+M, 4–6d):** add the `--admin` patterns to the deny list (S); enforce an equivalent floor in every adapter wrapper (M). **Owner: FX — pipeline-buildable as a story once designed.**

### SEC-3 · `sdlc repair` trusts any root that isn't literally `.claude/worktrees/*` — 🔴 High (already exploited: #630)
`repair.py:41-53 is_worktree_root` is a path-segment glob; `default_repo_root()` (`:243-266`) accepts any `__file__`-derived path; `_inspect()` (`:145-166`) never checks `src.exists()`. Observed: a `/private/tmp` checkout repointed all 10 managed `~/.claude` symlinks; when `/tmp` was cleaned every Claude Code session on the machine lost hooks and settings. The default repair then proposed relinking into an **empty** uv-tool lib and would have reported success. **Fix (S–M, 2–4d):** require source existence and an explicit allowlist (`$HOME`-rooted or a marker file written by the installer); refuse — don't plan — when `src` is missing. **Owner: FX.**

### ARCH-1 · `build.py` is a 9,297-line god-module — 🔴 High (structural)
7,213 code lines, cyclomatic complexity **614**. Holds: CLI parsing (`~2013-2176`), the entire `Ledger` class (**1,719 lines**, `2177-3896`, incl. schema DDL `:162` and migrations `:394`), rate-limit/cost gates (`~989-1730`), every stage's prompt renderer (`~4560-5313`), git/worktree plumbing (`~5388-6055`), merge-gate orchestration (`~4884-5144`), and the executor (`6326-7904`, `_run_story` alone ~600 lines). `docs/controller-architecture.md` documents the sprawl as intended. Every defect hit this session — the migration collision (#621), the status-marker/dirty-tree coupling, terminal-state-vs-approval — lives here. **Fix (L, 6–10d, staged):** extract `sdlc/ledger/` first as a pure move (schema, migrations, queries), then prompts, then git/worktree. **Owner: FX — pipeline-buildable in slices.**

### REL-1 · Captured dispatch path has no stall detection and no process-group kill — 🔴 High
Every non-`stream-json` adapter (codex/qwen/opencode/custom) takes `_dispatch_captured` (`dispatch.py:755-812`): a bare `subprocess.run(timeout=3600)` (`:282`). The 300s output-idle detector (`:302, :947`) and SIGTERM→SIGKILL group escalation (`_terminate_process_group`, `:347-368`) exist **only** on the streaming path — the docstring at `:709-713` says so. Observed: an unreachable OpenCode default provider printed a banner and sat silent; the only backstop was one hour **per stage**, ~5 hours per full run. **Fix (M, 2–3d):** route captured timeouts through `_terminate_process_group`; add a reduced wall-clock default or liveness probe; declare hang-detection as a capability in `harnesses.yaml` rather than an accident of stream format. **Owner: FX.**

### REL-2 · Ledger migrations are version-number-only; drift surfaces after paying for a stage — 🟠 High (already hit: #621)
`_apply_migrations` (`build.py:776-825`) skips any version present in `_migrations`; a ledger bootstrapped with `(1,'init')` skipped migration 1 forever. The six missing usage columns were discovered when `stage_set_usage` failed **after** an 18-minute build stage. No preflight compares live `PRAGMA table_info` to the expected schema. **Fix (M, 2–3d):** declarative expected-schema dict + `sdlc doctor` / run-start preflight; name-aware idempotency. **Owner: pipeline-buildable.**

---

## Architecture Assessment

**What's right.** The harness registry (`harnesses.yaml` + `harness.py`: `command`, `parser`, `{model}` placeholder, `models:` map, capability flags) is a clean abstraction; the ledger-as-truth / markdown-as-view intent is correct; `resume.py:190-260` reconstructs state as a pure read; `AWAITING_APPROVAL` as a first-class terminal state is the right primitive; `sanitize.py` is a genuine prompt-injection mitigation applied at the dispatch boundary (`dispatch.py:742`).

**What's wrong.**
- **Two state channels.** `story_markdown.py:47-90` stamps `**Status**: Done` into tracked `docs/stories/*.md` in the shared checkout mid-run, while SQLite is the truth. This is the root cause of the #590 dirty-tree guard making concurrent runs impossible without `--allow-dirty` — observed three times in one day. Derive the markdown read-only from the ledger, or write to a git-ignored side file.
- **Adapters are copies, not a parameterised implementation.** `scripts/{codex,qwen,opencode}-build-adapter.sh` were each hand-copied from `controller/adapters/generic-cli-adapter.sh` and diverged. A template regression (pipeline replacing `exec`, #624→#635) propagated into a new adapter within hours. The registry's promise — "adding a harness is config, not Python" — is undercut by N shell bodies that rot independently.
- **Two independent "is this root safe" implementations** (`repair.py:41` and a glob in `install/core.sh`) kept aligned only by a unit test. **No single resolution authority** for the repo root across two checkouts and a uv-tool install — the default resolved to an empty directory.
- **Dispatch safety is an accident of stream format**, not a declared capability (REL-1).
- **Scale ceiling is appropriate and should be stated:** single machine, single SQLite ledger, in-process Python, concurrency guard keyed on `(repo, scope)` with PID liveness (`registry.py:285`). Fine for one operator; not headroom for a multi-host service.

---

## Code Quality Audit

**Standards & typing.** Conventional commits enforced (`.commitlintrc.json`) — note the `footer-leading-blank` trap: any body line starting `word:` is parsed as a footer; it rejected two commits this session. Mypy ratchet is real but **strict on 33 of 66 modules**; `.mypy-baseline.json` freezes 31 violations concentrated in `build.py`/`cli.py`/`dashboard.py`/`resume.py` and is a plain editable file.

**Security posture (beyond P0s).** Dashboard binds `127.0.0.1` (`dashboard.py:280,1459,1509`) with a real traversal guard on `/log` (`resolve()`+`relative_to`), but **no auth of any kind** — any local process or a DNS-rebinding page can read full agent transcripts (S, 1d: per-run token or unix socket). Unpinned code execution: `mcp/config.template.json:3-9` (`npx -y …@latest` ×2), `scripts/install-controller.sh:44` (`curl … | sh`), `.github/workflows/eval-ci.yml:61` (`npm install -g` unpinned) (S, 1d). The container sandbox (`dispatch.py:543-582`: `--network none`, `--cap-drop ALL`, `no-new-privileges`) is well built but **opt-in**. Secrets hygiene is solid: `.env` untracked, gitleaks first in CI under `contents: read`, `.gitleaks.toml` narrowly scoped.

**Performance.** `dashboard.py:1398-1430 _serve_logs` reads **every stage attempt's full JSONL transcript into memory on one request** — the reproducible mechanism behind two dashboard OOM-kills this session (S, 1d: tail-cap + link to the confined `/log`). `events` table has only `idx_events_run_ts (run_id, ts)` (`build.py:265-268`) while `latest_progress` (`:3609-3639`) groups by `story_id` filtered on `level` — unindexed and unbounded (S, 0.5d). **53 unclosed-SQLite `ResourceWarning`s from `test_build.py` alone** — connections opened without a context manager (S, 1d).

**Cost control.** Bugfix budget is a flat per-stage counter (`MAX_BUGFIX_ATTEMPTS=2`, `build.py:104-114`); nothing caps cumulative story spend across stage re-entries. Observed: one 8-point story ran review×2/merge×2/bugfix×2; another burned **8h13m over four review rounds**, one round wasted on a stale checkout the reviewer read. The stale-packet bug is already fixed (`build.py:7816-7826`, #527) — the remaining cost driver is re-entry accumulation (M, 2–3d: story-level cumulative-token ceiling → `NEEDS_ATTENTION`).

**Testing.**

| Area | Exists | Gap | Risk |
|---|---|---|---|
| Controller logic | 4,138 tests, **99% line coverage** (measured) | `--cov` not in CI; no `--cov-fail-under` | High — silent regression |
| Typing | Ratchet + baseline | 50% of modules untyped-tolerant; baseline editable | Medium |
| Harness identity | Argv tests across 5+ files | `"claude" in token` substring guards (`test_codex_adapter.py:63,105,201`, `test_harness_routing_dispatch.py:128,154,256`, `test_build.py:321`) — broke once already when a model id was pinned | Medium — false breaks |
| Concurrency | `test_parallel_execution.py` | `time.sleep(hold)` ordering signal (`:72-89`, `:1151`) — flaked on a 2-CPU runner, passed on rerun | Medium — erodes trust in the gate |
| Shell layer | 48 bats files | `commitlint.bats` hard-requires `node_modules`; excluded from CI; fails locally instead of skipping | Low |
| Root docs | `doc_currency.py` exists | Not dogfooded on this repo | Medium (see below) |

---

## Business Risk Analysis

**Deployment readiness: pilot-ready.** The CI stack — gitleaks, shellcheck/static, mypy ratchet, supply-chain scan, contract checks, commitlint, bats, controller smoke on ubuntu+macOS, clean-machine install smoke, separate risk-gate workflow, semver auto-release — is unusually mature for a single-maintainer public repo. Two verified flakes/traps (`test_parallel_execution.py:1151`, commitlint footers) and the unbounded `controller-smoke` runtime (2m24s–8m56s against a 10-min cap, raised once already) are the operational rough edges.

**Maintenance burden: ~5–10 hrs/week** (estimate, UNVERIFIED — no time data). 943 commits, **632 (67%) agent-co-authored**; 11 releases in one session. Human time goes to agent-PR review, flaky-CI triage, risk-gate labelling, and doc upkeep — not typing code.

**Bus factor: 1, and the second reviewer is an LLM.** No co-maintainers in `git shortlog`. The review stage caught four real defects this session (two introduced by the assisting agent) against one stale-checkout false block and one false positive — good signal, but not independent human review. `docs/onboarding.md` is strong for *operating* the pipeline; a second engineer *modifying* the 66-module controller has `docs/controller-architecture.md` and 212 story files, not a maintainer-oriented map of `build.py`. The `risk-approved` label is self-applicable by the sole maintainer — an accepted tradeoff, worth stating in the docs.

**Documentation drift is structural, not incidental.** `docs/stories/STORIES.md` index stops at Epic-28 with Epics 29–31 shipped. `README.md:376-377` file counts were corrected on the morning of this review (24 scripts / 45 bats) and were **already wrong again by afternoon (27 / 48)** — the pipeline itself adds files. Nothing checks these. Given `onboarding.md` promises "the docs are wrong, not you," recurring drift undercuts the promise. An orphaned rule: `CLAUDE.md:118-120` (project *and* global) instructs aligning `nyx/package.json` with the tag — **no `nyx/` directory exists**; the real source is `controller/pyproject.toml`. Because CLAUDE.md is declared to override defaults, an agent following it literally during a release looks for a path that isn't there.

**Compliance.** No PII/payment handling. Transcripts and ledger are gitignored and never reach the public repo; home paths are scrubbed (`status.py:105`). Gap: no documented retention/purge policy for prompts and issue bodies persisted on each pilot user's machine (`.sdlc-state.db.logs/`). MIT licence, no conflicts; `skills/model-shelf` submodule is MIT (pinned to a fork pending upstream PR #10 — currency debt, not licence risk).

**Not yet built, gate before it ships:** Epic-30's `sdlc listen` webhook would let any public-repo issue author trigger agent dispatch. Its injection defenses (`epic-30-*.md:261-271`) are design intent with no code to review — **UNVERIFIED by construction**.

---

## Technical Debt Backlog (prioritised)

| # | Item | Evidence | Business impact | Effort | Priority |
|---|---|---|---|---|---|
| 1 | Risk gate reads policy/detector from PR checkout | `risk-gate.yml:45,58`; policy file unprotected (verified) | Flagship control bypassable by any contributor | M 3–5d | **P0** |
| 2 | No deny baseline on registry harnesses; `--admin` merge unblocked | `dispatch.py:88-186`, `harness.py:163-181`, `fix_issue.py:498-503` | Prompt injection → secret exfil or gate bypass | S+M 4–6d | **P0** |
| 3 | `sdlc repair` root trust is a path glob; no `src.exists()` | `repair.py:41-53,145-166,243-266`; #630 | Breaks every session on the machine | S–M 2–4d | **P0** |
| 4 | Captured dispatch: no stall detection, no group kill | `dispatch.py:282,709-713,755-812` | Silent hour per stage, orphaned CLIs | M 2–3d | **P0** |
| 5 | Extract `Ledger` + schema/migrations from `build.py` | `build.py:162-825, 2177-3896` | Isolates the surface behind #621; halves god-module | L 6–10d | **P1** |
| 6 | Schema-drift preflight (`PRAGMA table_info` vs expected) | `build.py:776-825`; #621 | Stops paying for a stage before failing | M 2–3d | **P1** |
| 7 | Dashboard `_serve_logs` unbounded multi-file read | `dashboard.py:1344-1361,1398-1430` | Reproducible OOM (2× today) | S 1d | **P1** |
| 8 | Story-level cumulative-spend circuit breaker | `build.py:104-114,7904-7932` | 8h13m/4-round stories | M 2–3d | **P1** |
| 9 | Status markers written to tracked docs mid-run | `story_markdown.py:47-90`; #590 | Blocks concurrent runs; dirties every run | M 2d | **P1** |
| 10 | Gate controller's own coverage in CI | `ci.yml` controller-smoke; 99% measured | Silent regression from a great baseline | S 0.5d | **P1** |
| 11 | Converge shell adapters onto one parameterised body | `controller/adapters/generic-cli-adapter.sh` vs `scripts/*-build-adapter.sh` | Template regressions propagate (#624→#635) | M 3d | **P1** |
| 12 | Dashboard auth (token or unix socket) | `dashboard.py:280,1182-1362` | Local transcript disclosure | S 1d | **P2** |
| 13 | Pin MCP/CI installs; drop `curl\|sh` or checksum it | `mcp/config.template.json:3-9`, `install-controller.sh:44`, `eval-ci.yml:61` | Supply-chain drift | S 1d | **P2** |
| 14 | `events` index `(run_id, level, story_id)` + retention | `build.py:265-268,3609-3639` | Dashboard scan cost grows per run | S 0.5d | **P2** |
| 15 | Close SQLite connections (context managers) | 53 `ResourceWarning`s in `test_build.py` | fd exhaustion on long unattended runs | S 1d | **P2** |
| 16 | De-brittle tests: structured asserts, event barrier not `sleep` | `test_parallel_execution.py:72-89,1151`; substring guards ×8 | Flakes + false breaks | M 2–3d | **P2** |
| 17 | Auto-poll `AWAITING_APPROVAL` (opt-in, bounded) | `build.py:322-337` | Overnight runs read as hung | S 1d | **P2** |
| 18 | Single repo-root authority (marker/env) for installer & repair | `repair.py:243-266`; empty uv-tool lib | Repair into a void | M 2d | **P2** |
| 19 | Dogfood doc-currency on README/STORIES.md; fix `nyx/` rule | `README.md:376-377`, `STORIES.md:59`, `CLAUDE.md:118-120` | Onboarding trust; agent follows a dead rule | S–M 1–2d | **P2** |
| 20 | Promote remaining 33 modules to mypy rung 1 | `pyproject.toml` overrides, `.mypy-baseline.json` | Runtime type errors in half the tree | M 3–5d | **P3** |
| 21 | Retention/purge policy for local transcripts | `.sdlc-state.db.logs/` | Compliance hygiene for pilot users | S 0.5d | **P3** |

---

## Recommendations Matrix

| Issue | Impact | Effort | Priority | Owner |
|---|---|---|---|---|
| Evaluate risk-gate detector + policy from base ref; protect the policy file | Restores the only merge-time human gate | M | P0 | FX (manual — it *is* the gate) |
| Uniform deny floor across harnesses; block `gh/glab … --admin` | Structural privilege ceiling instead of prompt convention | S+M | P0 | FX design → pipeline story |
| `sdlc repair`: source must exist and sit under an allowlisted root | Prevents machine-wide config hijack | S–M | P0 | FX |
| Captured-path stall detection + group kill; declare as capability | Bounds worst-case cost per stage from 1h to minutes | M | P0 | pipeline story |
| Extract `sdlc/ledger/` (pure move first) | Reviewable ledger; halves `build.py`; unblocks #6 | L | P1 | pipeline story, sliced |
| Schema-drift preflight in `doctor` and run start | Fail before spend, not after | M | P1 | pipeline story |
| Cap dashboard transcript payloads | Ends the reproducible OOM | S | P1 | pipeline story |
| Story-level cumulative-spend breaker | Caps runaway review/bugfix loops | M | P1 | pipeline story |
| Derive markdown status from ledger (or git-ignored side file) | Enables concurrent runs; kills `--allow-dirty` | M | P1 | pipeline story |
| `--cov-fail-under=90` on controller-smoke | Locks in 99% for free | S | P1 | FX (5 minutes) |
| One parameterised adapter body | Stops template rot propagating | M | P1 | pipeline story |
| Dashboard auth; pin MCP/CI installs | Closes local disclosure + supply-chain drift | S | P2 | pipeline story |
| Test de-brittling; `events` index; connection hygiene; approval auto-poll | Reliability & CI trust | S–M each | P2 | pipeline stories |
| Doc-currency CI check; fix `nyx/` rule; index Epics 29–31 | Onboarding trust; correct agent guidance | S | P2 | FX |
| Gate Epic-30 `sdlc listen` on a real injection-boundary review | Public issue → agent dispatch is a new attack surface | — | before ship | FX |

---

## What is already good (credit where due)

`sanitize.py` (zero-width/bidi stripping, HTML/script/data-URI neutralisation, fence-aware, severity-weighted) applied at the dispatch boundary · Claude-harness deny baseline as a floor under `--dangerously-skip-permissions` · opt-in container sandbox done properly · secrets never tracked, gitleaks first in CI under least privilege · `worktree-gc.sh` deletes only `agent-*` worktrees, respects locks, requires full merge · risk-gate uses `pull_request` not `pull_request_target` · path-confined transcript serving · 99% measured coverage · a review agent that caught four real defects in one day, two of them introduced by the assistant · every finding in this document was reproducible from the repo, which is itself a sign of a well-instrumented system.

---

*Effort key: S = 1–2 engineer-days, M = 3–5, L = 6–10. "Pipeline story" means the item is well-specified enough to hand to `sdlc build` once written up as a story; "FX" means it needs a human decision or touches the controls that gate the pipeline itself.*
