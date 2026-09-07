# Investigation: a development queue for `sdlc`

**Status:** decided 2026-09-07 → **Epic-32** (`docs/stories/epic-32-development-queue.md`) · **Author:** FX +
Claude · **Decisions:** option B (host-level SQLite queue with leased claims); the queue owns approval
re-poll and auto-resume; queue built first, serial per repo, with the shared-checkout status markers moved
off the checkout as story 32.1-003 inside the epic rather than as an external blocker.

## 1. The question

Today `sdlc build <scope>` and `sdlc fix <issue>` are **imperative, foreground, one-run-per-invocation**
commands: whoever types them is the scheduler. The proposal is to make them **enqueue** work into a durable
queue, and let a scheduler drain it — across scopes, and across repositories — subject to the constraints
the controller already knows about.

The last 48 hours are the motivating evidence. Twelve stories across two epics were shipped by a human
typing `sdlc build`, waiting, labelling, typing `sdlc resume`, and — overnight — by a **shell `for` loop**
(`~/sdlc-overnight-2026-09-07.sh`) that is a queue with none of a queue's guarantees: no persistence, no
priority, no cross-repo awareness, and a dirty-tree guard implemented by stashing files by name.

## 2. What already exists (the queue is half-built)

The codebase contains four partial schedulers. A queue should compose them, not replace them.

| Mechanism | Where | What it already does | Scope |
|---|---|---|---|
| **Ready-queue dispatch** (Epic-24, 24.1-001) | `build.py` executor, `ThreadPoolExecutor(max_workers)` (`build.py:1933`) | Maintains a ready set (deps ⊆ done) and dispatches to any free worker; no wave barrier | **within one run** |
| **File-overlap serialisation** | `fix_issue.py:2334 build_overlap_dependencies` | Batch `fix all` investigates every issue first, builds a file-overlap graph over `files_to_modify`, synthesises dependencies so overlapping issues serialise and independent ones run concurrently; bugs before enhancements | **within one batch run** |
| **Host registry** (11.2-001) | `registry.py` → `$XDG_STATE_HOME/sdlc/registry.json`, atomic JSON | Records every run on the host: `run_id, repo, db, scope, pid, status, started_at, finished_at, total, completed`. `find_live_owner` guards `(repo, scope)` with pid liveness. **Already spans 10 repos on this machine.** | **across repos, across runs** — but discovery-only |
| **FIFO trigger queue** (Epic-30, 30.2-002, unbuilt) | `sdlc listen` design | Webhook triggers queued FIFO, one live run per repo via the registry, queue persisted in JSON beside the pidfile, survives restart | **across runs, one repo** |

Missing between them: a **durable, host-level list of work-to-do** with an owner that drains it. Everything
else — dependency resolution, overlap detection, per-run parallelism, cross-repo discovery — is present.

## 3. The constraints a queue must respect

These are facts of the current controller, each verified in code or in operation this week.

1. **Fix jobs are exclusive per repo.** `fix_issue.py:1767`: *"A fix run never cuts a worktree: every stage
   runs against the repo root."* Two fix jobs in one repo cannot overlap. (This week's incident: a human
   `git switch` in the repo root moved HEAD off the agent's branch mid-run.)
2. **Build jobs parallelise within a repo via worktrees** (`create_story_worktree`, `build.py:5911`) — but two
   concurrent *runs* in one checkout are blocked by the #590 dirty-tree guard, because the controller writes
   `**Status**: Done` markers into `docs/stories/*.md` in the shared checkout (REVIEW.md item 9). Until the
   markers move off the checkout, the rule is **one run per repo at a time, N stories inside it**.
3. **The rate-limit window is per account, discovered per run.** `RATE_LIMITED` is persisted in the
   **per-repo ledger** (`build.py:1189,1259`); the registry carries no rate-limit state. Two repos hit the
   same wall separately and each waits up to 5h alone. A queue must treat the Max window as **one shared
   resource**: when any job parks on it, nothing else should start until the reset.
4. **Host memory is finite and already binding.** This machine OOM-killed the dashboard twice and a live
   build once in one day, with a 27B local model loaded. Agent slots must be a **host-level** cap, not a
   per-run one.
5. **`AWAITING_APPROVAL` is terminal within a run** (`build.py:322`). A label arriving later needs a human
   to `sdlc resume`. Three of the four P0 fixes this week parked here. A queue that re-polls approval and
   resumes by itself removes the single most repetitive human step in the loop.
6. **The installed controller drifts from `main`.** 15.1-004 now warns per run; a queue should check per
   **job** and refuse (or reinstall) rather than run twelve releases behind, as happened on 2026-09-06.
7. **Per-repo ledgers stay the truth for runs.** A queue is a *coordination* layer; it must not become a
   second source of run state (the exact failure mode of the markdown status markers).

## 4. Cross-repo parallelism, specifically

Different repos are naturally independent: separate checkouts, separate ledgers, separate worktrees, separate
PR streams. **They can run in parallel today** — nothing in the controller forbids it; the registry already
lists them side by side. What they contend for is:

| Shared resource | Today | Under a queue |
|---|---|---|
| Max rate-limit window | each run discovers it alone | one host-level `RATE_LIMITED` state; scheduler pauses all dispatch until reset |
| Host memory / agent slots | `--concurrency` is per run | host-level slot count (e.g. 4 agents) allocated across jobs |
| GitHub/GitLab API rate | per process | per account — probably fine, worth a counter |
| The `sdlc` binary | one install for all repos | per-job version check (15.1-004) before claiming |
| The human | labels + resumes | approval re-poll; Telegram on park/finish |

Same-repo is where the real rules live: fix jobs exclusive; build jobs one run at a time until item 9 lands,
then N; file-overlap graph extended from "issues in one batch" to "jobs in one repo's queue".

## 5. Options

### A. Extend `registry.json` with a `queue` section
Smallest change: pending jobs appended to the existing atomic JSON; `sdlc build --enqueue` writes, a
`sdlc queue run` loop reads. **Pro:** no new store, reuses `find_live_owner`. **Con:** JSON with
whole-file rewrite is fine for tens of records, poor for claims/leases; no room to grow to a second host.

### B. Host-level SQLite queue (recommended)
`$XDG_STATE_HOME/sdlc/queue.db` (WAL), one `jobs` table: `id, repo, kind(build|fix), scope, priority,
state(queued|claimed|running|parked|done|failed|cancelled), claimed_by, lease_until, run_id, created_at,
reason`. Workers **claim** with a short lease (Hyqs uses 90s renewed every 30s — the same pattern at
single-host scale), so a dead scheduler's job is reclaimable. Per-repo ledgers remain run truth; `run_id`
links the two. **Pro:** durable, concurrent-safe, matches the ledger's own technology, grows to a second
host by swapping the file for Postgres without changing the model. **Con:** one more state file; `doctor`
must learn it.

### C. Scan the per-repo ledgers, no new store
A coordinator that reads every registered ledger and decides what to start. **Pro:** zero new state.
**Con:** cross-repo *ordering* and *priority* have no home; "queued but not yet a run" is not a ledger
concept; it reinvents B badly.

**Recommendation: B**, with the scheduler loop designed as the same process Epic-30's `sdlc listen`
supervisor will become — webhooks *enqueue*, the same drainer *dispatches*. One daemon, two intakes.

## 6. Proposed shape (for `/create-epic`)

**Epic-32: Development Queue** — *"`sdlc build` and `sdlc fix` enqueue; one scheduler drains across
repos under the host's real limits."*

| Story | Points | Depends on |
|---|---|---|
| 32.1-001 Queue store + `sdlc queue add/list/cancel/prioritise`; `build`/`fix --enqueue` (foreground remains the default until 32.1-002 is trusted) | 5 | — |
| 32.1-002 Scheduler loop: claims with leases, per-repo exclusivity (fix) / one-run-per-repo (build), host agent-slot cap; `sdlc queue run` foreground first, daemon later | 8 | 32.1-001 |
| 32.2-001 Shared rate-limit state: any `RATE_LIMITED` parks the queue; auto-resume at reset | 3 | 32.1-002 |
| 32.2-002 Approval-aware queue: `AWAITING_APPROVAL` jobs re-polled; label → auto-resume; Telegram on park | 3 | 32.1-002 |
| 32.3-001 Policy: priority classes (P0/bug first, then features), per-job budgets by class (fix rounds, wall cap), file-overlap serialisation across a repo's queue | 5 | 32.1-002 |
| 32.3-002 Dashboard queue panel; per-job controller version check; `doctor` reports queue health | 3 | 32.1-001 |

≈ 27 points. **Hard dependency:** REVIEW.md item 9 (status markers off the shared checkout) before
same-repo build jobs may overlap; without it the queue is still correct, just serial per repo.
**Convergence:** Epic-30 `sdlc listen` becomes an intake into this queue rather than owning its own.

## 7. What the queue does *not* solve

- Review quality, retry loops, and per-story cost — those are the pipeline's, and the cumulative-spend
  breaker (REVIEW.md item 8) belongs there, though 32.3-001 gives it a per-job home.
- Multi-host execution — deliberately out of scope; option B leaves the door open.
- The human gate — approvals stay human; the queue only stops making the human also be the resume button.

## 8. Reference

Hyqs (Clément Milville) runs the multi-host version of option B: a Postgres job table, symmetric workers
claiming with 90s leases, an advisory-lock-elected supervisor, per-class retry budgets, and a
deploy-only host. Its economics page reports 2,673 changes in 68 days with 23.5% ending in human review.
This proposal is that design at single-host scale, on SQLite, keeping this controller's existing
per-repo ledgers and worktree model intact.
