<!-- ABOUTME: Comparative analysis of Hyqs (clementmilville/hyqs) against this repo's sdlc controller. -->
<!-- ABOUTME: Lists the mechanisms worth adopting, ranked, with the epic/issue each one maps to. -->

# Hyqs vs `claude-code-config` — comparative analysis

**Date:** 2026-09-23 · **Subject:** [hyqs.milville.com](https://hyqs.milville.com/how-it-works/) and [github.com/clementmilville/hyqs](https://github.com/clementmilville/hyqs) · **Against:** this repo at v2.71.1 (`sdlc` controller, `autonomous-sdlc` plugin)
**Method:** read every `how-it-works` page (pipeline, workers, intake, evidence, economics, deploy, reference, get-started), the Hyqs README and repo tree, and four source modules (`gate_guard.py`, `personas.py`, `conflict_autoresolve.py`, `limits.py`); cross-checked each mechanism against `controller/src/sdlc/`, `docs/controller-architecture.md`, `REVIEW.md` and the open issue list. Hyqs figures are self-reported from its own ledger (6 Sep 2026) and were not reproduced.

---

## 1. Verdict in thirty seconds

Hyqs and this repo solve the same problem — an unattended plan → build → gate → merge loop driven by Claude — with one big architectural difference: **Hyqs is a hosted, multi-project service** (Postgres, symmetric workers on any host, RBAC, signed container deploys), whereas this repo is deliberately **a per-repo controller on one operator's machine** (SQLite ledger, host queue, forge as the source of truth). Our own host-runtime architecture doc says not to move orchestration into a runtime service; that decision stands. Nothing here argues for a Postgres rewrite.

Within the loop itself, Hyqs is cleverer in one area and we are ahead in several:

- **Hyqs is cleverer at making safety deterministic.** Read-only roles lose `Write`/`Edit` at the SDK, every gate runs inside a snapshot-and-restore guard, gates judge only the changed files, retry budgets are isolated per failure class, and the commonest merge conflict is resolved without a model. Each of these replaces a prompt instruction or a deny-list pattern we rely on today with a check that cannot be argued with.
- **We are ahead** on cross-harness portability (Claude / Codex / Qwen / OpenCode; Hyqs is Claude-SDK-only with a Codex prompt prefix), forge neutrality (GitHub and GitLab; Hyqs is GitHub-only), the human high-risk gate with `AWAITING_APPROVAL`, envelope re-ask and commit-lint-by-construction, close-out reconciliation against `origin/main`, the evaluation harness with A/B baselines, model routing with empirical escalation, and test discipline (4,138 tests at 99% coverage; Hyqs shows eight squash commits and a 372 KB `store.py`, so its god-module problem is larger than our `build.py`).

**Recommendation:** adopt seven mechanisms, all inside the existing controller, none requiring a new runtime. Ranked by leverage per engineer-day they are: the gate guard, SDK-level tool removal for read-only roles, a deterministic lint stage scoped to changed files, structured review findings with path-classified checklists, per-class retry budgets with alternating-verdict escalation, deterministic pure-append conflict resolution, and decision records per merged story. Total estimate: **12–18 engineer-days**, every item pipeline-buildable as a story.

---

## 2. Side-by-side

| Dimension | Hyqs | This repo | Edge |
|---|---|---|---|
| Runtime | Python 3.11, Starlette + React console, **Postgres** job store, systemd `--user` units, Docker | Python 3.11, Typer CLI, **SQLite** per-repo ledger + host registry + host queue (WAL), dashboard over SSE | Even — different scale targets |
| Agent runtime | Claude **Agent SDK** in-process; Codex as a prompt-prefixed second provider | `claude -p` subprocess by default; **config-driven harness registry** (claude, codex, qwen, opencode) | **Ours** |
| Forge | GitHub only | GitHub and GitLab (PR/MR, CI gate, issue mirror) | **Ours** |
| Stages | PLAN → BUILD → **LINT → TEST** → REVIEW → SECURITY → (DESIGN) → MERGE → DEPLOY | preflight → discovery → BUILD → COVERAGE → REVIEW → MERGE; security and lint live in CI at the merge gate | Hyqs (deterministic gates before the AI review) |
| Gate scope | "Judged on the files it changed, never on pre-existing debt" — per gate | Coverage pre-check measures changed-file coverage; review packet carries changed files + diff; CI runs repo-wide | Hyqs |
| Read-only roles | `Write`/`Edit`/`NotebookEdit` **removed at the SDK**; gate guard snapshots the worktree, discards a mutation, re-runs once, then fails | `DESTRUCTIVE_DENY_FLOOR` (`rm`, `git reset --hard`, …) via `--disallowedTools` for investigation/review/summary (issue #685); reviewers can still `Edit` | **Hyqs** |
| Coder containment | Worktree-confined writes, isolation guard flags out-of-tree mutation, 120-turn cap | Per-story worktree; escape **detected** (#612), containment sandbox opt-in (#614 open); no turn cap | Hyqs |
| Explorer / chat | bubblewrap sandbox, no Bash, no network | n/a | n/a |
| Review | Persona checklist selected by path class (db, frontend, authz, infra); schema-bound verdict with a **closed list of blocking reasons**; weakening a test is itself blocking | One generic prompt + docs-currency dimension; schema requires only `approval_status`/`change_count`/`final_status`, findings unstructured | Hyqs |
| Retry budgets | Isolated per class: 5 fix rounds, 5 rebase attempts (30 s → 10 min backoff), 2 stage timeouts, 1 plan re-ask, 3 DB retries; alternating review/security verdicts escalate to a human | `MAX_BUGFIX_ATTEMPTS=2` per story (all classes), envelope re-ask ×1, commit-lint re-ask ×2, per-run token budget; **no story-level cumulative cap** (REVIEW.md) | Hyqs |
| Merge conflicts | Pure two-sided append resolved **without a model**; else conflict-fix agent on the rebase budget | Merge agent rebases and resolves via the model every time | Hyqs |
| Plan sizing | >5 stories or >8 files ⇒ split into chained child jobs or parked | Story points from `/generate-epics`; predictor estimates tokens/rework; no footprint cap | Hyqs (cheap check) |
| Scheduling | DB advisory-lock leases (90 s / 30 s renew), symmetric workers, supervisor elected by session lock, 120 s scan, 17 failure classes with remediation, dead-letter after 5 requeues | Host queue with leases, slots, per-repo exclusivity, file-overlap serialisation, approval park, shared rate-limit pause; `sdlc listen` daemon in Epic-30 | Even for one host; Hyqs for a fleet |
| Rate limits | Typed `ProviderUnavailable`, job back to PENDING, provider idled until reset (max 6 h) | `RateLimitSignal`, story parked `RATE_LIMITED`, one shared host pause, auto-resume | Even |
| Cost | Usage ledger per model call incl. cache tokens; self-auditing report; unknown model priced zero **and flagged** | Stage usage from `stream-json`, `usage-reconcile` agreement rate, predictor, `sdlc eval` A/B | **Ours** (measurement), Hyqs (self-audit honesty) |
| Human gate | Escalation to "needs human" only | `risk:high` label + GitHub check, `AWAITING_APPROVAL` state, deterministic gate-block recognition, approval park with auto-resume | **Ours** |
| Recovery | Supervisor reconciles "merged but stuck" | `reconcile_run` verifies landing on `origin/main` by four signals; `rollback`, `repair`, `doctor` | **Ours** |
| Evidence | 9 record types; DB triggers on 13 tables stamp actor + `backend_pid`/`client_addr`; **one decision record per merged job in git**; 8 roles × 26 permissions | Ledger `events` with `source`, per-stage transcripts, dashboard; no decision records | Hyqs |
| Deploy | Probe-first cutover, build once / promote by digest, signed images, declared-not-read secrets, fix-forward (no rollback) | Out of scope: the pipeline stops at merge | n/a |
| Intake | Console, wave, MCP (38 tools, idempotency keys), interview with a nine-field deterministic gate, architect ≤10 jobs; "the queue takes no advice from the model" | `/project-init` → `/brainstorm` → `/generate-epics`; issue mirror; `/fix-issue all`; auto-fix trigger planned (#578) | Even |
| Tests / hygiene | Not visible (8 squash commits, `tests/` present) | 4,138 tests, 99% line coverage, ten-job CI incl. gitleaks and supply-chain scan | **Ours** |

---

## 3. Recommended amendments, ranked

Effort: XS < 0.5 d · S 0.5–1.5 d · M 2–4 d. Every item is a controller change; none touches the plugin surface.

### A1 · Gate guard: snapshot-and-restore around every read-only stage — **S**

**Hyqs.** `run_guarded_gate()` records `git status --porcelain -z` before a review/security gate, compares after, tolerates only lockfile regeneration, restores and re-runs once on any other mutation, then fails the job with `GateIsolationError`.
**Us.** Issue #685 showed a review agent `rm -f`-ing an untracked file. The fix was a deny floor on `rm`/`git reset`/… — a string-matched list that a compound command or an `Edit` call walks past. #614 (sandbox containment) is the heavyweight answer and is still open.
**Amend.** Wrap `_dispatch_stage` for `READ_ONLY_ROLES` (review, investigation, summary, adversarial, over-engineering lens) in a worktree fingerprint check — the `#607` `_checkout_fingerprint` helper already exists in `build.py`. On drift: `git checkout -- . && git clean -fd` inside the *story worktree only*, log an `error` event, re-dispatch once, then park `NEEDS_ATTENTION`. Deterministic, no model, no container. Complements #614 rather than replacing it.
**Maps to:** Epic-13 (agent runtime security), closes the residual of #685.

### A2 · Remove write tools from read-only roles at the CLI, and cap turns — **XS**

**Hyqs.** Planner and reviewer roles have `Write`/`Edit`/`NotebookEdit` removed at the SDK; "tool policies are decided by the SDK, not by the prompt". Coder capped at 120 turns.
**Us.** `resolve_deny_rules(role)` already appends a per-role floor. It blocks destructive shell but leaves `Edit`, `Write`, `MultiEdit`, `NotebookEdit` available to a reviewer.
**Amend.** Extend `DESTRUCTIVE_DENY_FLOOR` for `READ_ONLY_ROLES` with `Edit`, `Write`, `MultiEdit`, `NotebookEdit`. Add `--max-turns` per role (from `harnesses.yaml`, e.g. build 120, review 40, merge 30) as the outer cost breaker that REVIEW.md's "8h13m over four review rounds" finding asked for. Both are argv decorations in `resolve_agent_cmd`; the codex/qwen/opencode adapters already refuse host-auth roles without a deny baseline (#654), so the policy stays honest across harnesses.
**Maps to:** Epic-13, Epic-14 (cost governance).

### A3 · Deterministic LINT + TEST stage on changed files, before the review dispatch — **M**

**Hyqs.** Lint (lockfile drift, `ruff`/`prettier`/`eslint` on changed files, 5-min cap) and Test (suite + import smoke + migration-head check, 15-min cap) are pure subprocesses run *before* any reviewer token is spent; a job with no detectable test suite is refused. Review consumes 13% of Hyqs tokens because it only ever sees green diffs.
**Us.** `coverage_precheck.py` already runs the project's test + coverage command deterministically and skips the coverage agent when there is no gap. There is no lint equivalent, so a formatting or lockfile slip reaches the reviewer (a model call) or CI (after the PR is open).
**Amend.** Generalise the pre-check into a `gates.py` stage runner: detect `ruff`/`eslint`/`prettier` from the repo and run them on `git diff --name-only origin/main...HEAD`; run `uv lock --check` / `npm ci --dry-run` when a lockfile is tracked; a red result routes to the bugfix agent with the tool's output as the finding — the existing `CODE_BUG`/`TEST_BUG` path — instead of to review. Record it as a `lint` stage row so the dashboard shows it. Keep it changed-files-only so pre-existing debt never blocks a story (Hyqs's convergence argument).
**Maps to:** Epic-27 (token optimisation); closes the "security gate runs only in CI" gap in `docs/security-gates.md` for lint.

### A4 · Structured review findings with a closed blocking-reason list and path-classified checklists — **M**

**Hyqs.** `personas.py` appends a db / frontend / authz / infra checklist to the review prompt based on the diff's paths, assembled in fixed order. The verdict schema has a closed list of blocking reasons; "weakening an existing test is itself a blocking finding"; edits to the project constitution get heightened scrutiny.
**Us.** `render_review_prompt` is one generic prompt plus the docs-currency dimension. `review-agent-response.schema.json` requires no findings at all, so a `CHANGES_NEEDED` verdict carries no machine-readable *why*, and the bugfix agent's `finding_dispositions` has nothing typed to dispose of.
**Amend.** (1) Add `findings[]` to the review schema: `{path, line?, reason ∈ enum, severity ∈ {block, warn}, summary}` with a closed `reason` enum (`test-weakened`, `security`, `correctness`, `missing-test`, `contract-drift`, `docs-stale`, `over-engineering`, …); require ≥1 `block` finding when `approval_status == CHANGES_NEEDED`. (2) Add a `review_personas.yaml` keyed by path globs, assembled deterministically by the same matcher `change_class.py` and `high-risk-patterns.yaml` use, and inject the matching checklists into the prompt. (3) Make "a diff that deletes or loosens an assertion" a checklist item that must be reported as `test-weakened`. This gives the bugfix loop typed input and makes the review verdict auditable.
**Maps to:** Epic-18 (agent output quality), Epic-26 (process discipline).

### A5 · Per-class retry budgets, story-level spend ceiling, alternating-verdict escalation — **M**

**Hyqs.** Budgets are isolated per failure class so one class cannot exhaust another; rebase retries back off 30 s → 10 min; "alternating review/security verdicts escalate to a human ruling"; 17 failure classes each map to a fixed remediation.
**Us.** One `MAX_BUGFIX_ATTEMPTS=2` covers build, coverage, review and merge failures alike; the envelope and commit-lint re-asks are separately bounded (good). REVIEW.md documented a story that ran review ×2 / merge ×2 / bugfix ×2 and another that burned eight hours across four review rounds; there is no cumulative per-story ceiling.
**Amend.** Introduce a `Budgets` dataclass on `BuildOptions`: `fix_rounds` (per story, default 3), `rebase_attempts` (default 3, exponential backoff), `stage_timeouts` (default 2), plus `story_token_ceiling` (default derived from the predictor's estimate × 3, floor 2 M). Track each in the ledger `stages` rows (columns exist for attempts). Add the escalation rule: two consecutive review re-entries whose `findings[].reason` sets are disjoint ⇒ park `NEEDS_ATTENTION` with both verdicts in the event — the model is oscillating, not converging. Expose all of it in `sdlc status`.
**Maps to:** Epic-12 (robustness), Epic-14 (cost governance); the story-ceiling item is the open REVIEW.md "cumulative spend" gap.

### A6 · Deterministic pure-append conflict resolution before the merge agent — **S**

**Hyqs.** `conflict_autoresolve.py`: a conflict hunk where both sides are non-empty, of different lengths, with no `|||||||` ancestor block, is resolved as ours-then-theirs with zero model involvement; anything else goes to the conflict-fix agent. Every file must qualify or nothing is written.
**Us.** Every rebase conflict is handed to the merge agent. In this repo the dominant conflict shape is exactly pure append: parallel stories all touch `CHANGELOG.md`, `STORIES.md` and the epic file's status lines. Each one costs a merge-agent round and, on failure, a bugfix round.
**Amend.** Port the ~150-line pure function into `sdlc/conflict_autoresolve.py` with its rejection rules verbatim, call it from the merge stage after `git rebase origin/main` fails and before dispatching the merge agent; on full success continue the rebase, on any rejection fall through unchanged. Add the CHANGELOG / stories fixtures as tests.
**Maps to:** Epic-17/24 (parallel execution) — the mechanism that makes parallel cohorts cheap.

### A7 · Decision record per merged story, committed to git — **S**

**Hyqs.** One markdown file per merged job under `.hyqs/decisions/`; the architect grounds new plans in "the last fifteen decision records". It is the only evidence record that lives in the repo rather than the database.
**Us.** Rationale lives in PR bodies, ledger events and transcripts — none of which a future build agent reads. Epic-16 (continuous learning, stories 16.1-001/002 still open) wants exactly this substrate.
**Amend.** Have the merge stage (controller-side, deterministic) write `docs/decisions/<story-id>.md` from the ledger: story, PR/MR, root causes from any bugfix rows, review findings, files touched, tokens/cost. Commit it with the merge. Inject the last N records into the build and review prompts the way `Story.section` is injected today (bounded by the same 8 k-char rule).
**Maps to:** Epic-16.

### Smaller items worth a story each

| # | Mechanism | Effort | Note |
|---|---|---|---|
| B1 | **Footprint cap after build** — park a story whose diff exceeds N files / N LOC (Hyqs: 8 files) as `NEEDS_ATTENTION` instead of sending an oversized diff to review | XS | Reuse `change_class.py`'s `git diff --name-only`; default N=15 files, override in `.sdlc-change-class.yaml` |
| B2 | **Wedge and stale-job alerts in `sdlc queue run`** — all slots busy and no stage advanced in 5 min ⇒ one Telegram alert per episode; job older than 24 h ⇒ alert | XS | Hyqs supervisor items 2 and 4; the queue loop already has the data |
| B3 | **Unknown model priced zero and flagged** in usage accounting; longest-prefix match for dated snapshots | XS | Check `cost_estimate.py` / `usage.py`; Hyqs's "never guessed" rule keeps the ledger honest |
| B4 | **SQLite triggers on `stories.status` / `runs.status`** appending to `events` with `source='trigger'` so a hand edit to the ledger is visible to `sdlc doctor` | S | Hyqs audits 13 tables; we need two |
| B5 | **Idempotency key on `sdlc queue add` / `--enqueue`** and a 10 s dedup window, ahead of the Epic-30 webhook daemon (#577/#578) | S | Hyqs's MCP `create_job` semantics; #578 already lists idempotency as a requirement — this is the primitive it needs |

---

## 4. Where this repo is ahead — keep, do not "harmonise"

- **Harness registry** (`harnesses.yaml`, parsers, capability flags, degradation matrix). Hyqs binds to the Claude Agent SDK and bolts Codex on as a prompt prefix "with the same guards applied post-hoc". Our design is the more portable one; the Hyqs lesson is only that the *policy* must not depend on the harness — which #654 already enforces.
- **Forge neutrality and the human gate.** GitHub + GitLab, `AWAITING_APPROVAL`, deterministic gate-block recognition, approval park with auto-resume. Hyqs has one "needs human" bucket and no labelled high-risk path detection.
- **Envelope re-ask and commit-lint by construction.** Hyqs treats malformed output as "needs human"; we recover it for one cheap call.
- **Reconciliation against `origin/main`** by four independent landing signals. Hyqs's supervisor reconciles "merged but stuck" against its own DB.
- **Empirical routing and evaluation** — predictor, Balanced profile with escalation on predicted rework, `sdlc eval` A/B with regression baselines. Hyqs routes by a per-project roster you edit by hand.
- **Test discipline.** Nothing in Hyqs's public repo shows its coverage; ours is measured.

## 5. Not worth adopting

- **Postgres, symmetric workers, supervisor election, RBAC (8 × 26).** These solve a multi-tenant hosted service. `docs/sdlc-host-runtime-architecture.md` already decided orchestration stays in the repo and on the operator's Mac; the Epic-32 queue plus the Epic-30 daemon cover the single-host case. Revisit only if the pilot grows past one host.
- **DEPLOY stage** (probe-first cutover, digest promotion, image signing, fix-forward). Right design for a platform; this repo's contract ends at a merged PR. If a target project needs it, it is a project-level `podman-compose` skill, not a controller stage.
- **Explorer bubblewrap sandbox and the console job-chat.** No equivalent surface here; the dashboard is read-only by construction.
- **Hardware accounting via transient systemd scopes.** Linux-only; the primary hosts are macOS.
- **"The queue takes no advice from the model"** (a human clicks to file every job). Our posture is the opposite by design — `/fix-issue all`, the auto-fix trigger. Keep ours, but borrow the idempotency primitive (B5).

## 6. Caveats on the source

Hyqs's public repository is eight squash commits from one author with no stars, no issues and no visible CI beyond a secrets scan; the 68-day production figures (2,673 changes, $3.69 per change, 881 self-heals) come from its own ledger page and cannot be independently checked. The pipeline package is `store.py` 372 KB, `supervisor.py` 130 KB, `runner.py` 71 KB — the same god-module shape REVIEW.md flagged in our `build.py`, at three times the size. The mechanisms above were chosen because each is small, pure and independently testable, not because Hyqs as a whole is a better codebase.

## 7. Proposed issue list (ready for `/create-issue`)

1. `feat(dispatch): snapshot-and-restore gate guard around read-only stages` — A1, S
2. `feat(dispatch): deny Edit/Write tools and cap turns for read-only roles` — A2, XS
3. `feat(build): deterministic lint+lockfile stage on changed files before review` — A3, M
4. `feat(contracts): structured review findings with closed reason enum and path-classified checklists` — A4, M
5. `feat(build): per-class retry budgets, story token ceiling, alternating-verdict escalation` — A5, M
6. `feat(merge): resolve pure two-sided-append conflicts without a model` — A6, S
7. `feat(merge): write a decision record per merged story and feed it to build/review prompts` — A7, S
8. `feat(build): footprint cap on the built diff` — B1, XS
9. `feat(queue): wedge and stale-job alerts` — B2, XS
10. `fix(usage): price unknown models at zero and flag them` — B3, XS
11. `feat(ledger): status-change triggers into events` — B4, S
12. `feat(queue): idempotency key and dedup window on enqueue` — B5, S
