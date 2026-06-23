# ABOUTME: Controller-native crash-resume from the SQLite ledger (Story 10.1-001).
# ABOUTME: Recomputes the queue, re-enters the 4-stage loop at the interrupted stage.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sdlc.build import (
    _STAGES,
    BuildOptions,
    Dispatcher,
    Ledger,
    _budget_exceeded,
    _CostGatePause,
    _dispatch_cohort,
    _honor_parked_reset,
    _make_rate_limit_context,
    _prepare_story_workdir,
    _reposition_head,
    _resolve_dispatch,
    _run_story_rate_limited,
    _StoryRunOutcome,
    apply_budget_stop,
    apply_cost_gate_pause,
    apply_rate_limit_park,
    effective_concurrency,
    finalize_run,
    persist_cohort_structure,
)
from sdlc.cohort import Story, compute_cohorts
from sdlc.discovery import discover_queue
from sdlc.dispatch import dispatch_agent
from sdlc.notify import notify
from sdlc.registry import Registry

__all__ = ["StoryResumeState", "ResumeResult", "compute_resume_plan", "run_resume"]

# Story statuses the controller treats as already finished — never re-run.
_TERMINAL_STORY_STATES = {"DONE", "SKIPPED"}


@dataclass
class StoryResumeState:
    """How far one story got before the run was interrupted.

    Derived purely from the ledger so resume is deterministic: ``next_stage`` is
    the first pipeline stage without a recorded DONE attempt (None when the
    story's pipeline is complete), ``start_attempt`` continues the attempt count
    past any crashed/failed attempt of that stage, and ``pr_number`` /
    ``bugfix_seq`` carry forward so review/merge and the bugfix loop resume
    intact. ``start_escalation`` (Story 14.2-003) is ``next_stage``'s prior
    FAILED-attempt count, so cheap-first model escalation resumes on the tier the
    stage had already climbed to rather than dropping back to its cheap base.
    """

    story_id: str
    status: str
    done_pipeline_stages: frozenset[str]
    next_stage: str | None
    start_attempt: int
    start_escalation: int
    pr_number: int | None
    bugfix_seq: int


@dataclass
class ResumeResult:
    """The outcome of a resume invocation (mirrors :class:`BuildResult`)."""

    run_id: str | None
    resumed: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    needs_attention: int = 0
    awaiting_approval: int = 0
    skipped: int = 0
    nothing_to_resume: bool = False
    story_status: dict[str, str] = field(default_factory=dict)
    # Story 14.1-001: a resumed run honours the same budget ceiling (carried in
    # the ledger config, raised via `sdlc resume --budget`). Set when the gate
    # re-halted dispatch so the caller can report it like a fresh build.
    budget_stopped: bool = False
    budget_policy: str = ""
    accrued_tokens: int = 0
    notional_cost_usd: float = 0.0
    # Story 14.1-003: set when the resume re-hit the rate-limit window beyond the
    # auto-wait cap and durably re-parked RATE_LIMITED (resumable again later).
    rate_limited: bool = False
    rate_limit_reset_at: float | None = None
    rate_limit_waited_s: int = 0
    # Story 14.1-002: set when the interactive cost gate re-halted the resume. The
    # run is left IN_PROGRESS (resumable); raise --cost-threshold to continue.
    cost_gated: bool = False


def _pipeline(skip_coverage: bool) -> list[str]:
    """The active stage pipeline, honouring this run's coverage-gate setting."""
    return [s for s in _STAGES if not (s == "coverage" and skip_coverage)]


def compute_resume_plan(
    ledger: Ledger, run_id: str, *, skip_coverage: bool = False
) -> dict[str, StoryResumeState]:
    """Read the ledger and compute the per-story resume point for ``run_id``.

    A pure read: it never writes. For each story it determines which pipeline
    stages already have a DONE attempt, the first stage still owed, the attempt
    number to resume that stage at (one past the highest recorded attempt, so a
    crashed IN_PROGRESS attempt is not overwritten), and the carried-forward PR
    and bugfix sequence.
    """
    pipeline = _pipeline(skip_coverage)
    breakdown = ledger.stage_breakdown(run_id)
    plan: dict[str, StoryResumeState] = {}

    for row in ledger.story_rows(run_id):
        sid = row["story_id"]
        attempts = breakdown.get(sid, [])
        done_stages = {a["name"] for a in attempts if a["status"] == "DONE"}
        done_pipeline = frozenset(s for s in pipeline if s in done_stages)

        next_stage = next((s for s in pipeline if s not in done_stages), None)
        if next_stage is not None:
            stage_attempts = [a["attempt"] for a in attempts if a["name"] == next_stage]
            start_attempt = (max(stage_attempts) + 1) if stage_attempts else 1
            # Story 14.2-003: each prior FAILED attempt of the resumed stage
            # represents one cheap-first tier bump already made, so resume routes
            # on the tier the stage had climbed to. Only FAILED attempts count —
            # a crashed/rate-limited IN_PROGRESS attempt never escalated, so it
            # must not inflate the level.
            start_escalation = sum(
                1
                for a in attempts
                if a["name"] == next_stage and a["status"] == "FAILED"
            )
        else:
            start_attempt = 1
            start_escalation = 0

        # The "bugfix", "reask" and "commitlint" stages share the monotonic
        # ``bugfix_seq`` counter for their attempt number (Stories 12.1-001,
        # 12.2-002). A recovery stage that *succeeded* (e.g. a "reask" or a
        # "commitlint" amend) leaves its own row but no "bugfix" row, so
        # reconstructing the counter from a subset of these names would reuse an
        # existing attempt and collide on the stages PRIMARY KEY — resume must
        # continue past the highest of all three.
        recovery_attempts = [
            a["attempt"]
            for a in attempts
            if a["name"] in ("bugfix", "reask", "commitlint")
        ]
        bugfix_seq = max(recovery_attempts) if recovery_attempts else 0

        plan[sid] = StoryResumeState(
            story_id=sid,
            status=row.get("status", "TODO"),
            done_pipeline_stages=done_pipeline,
            next_stage=next_stage,
            start_attempt=start_attempt,
            start_escalation=start_escalation,
            pr_number=row.get("pr_number"),
            bugfix_seq=bugfix_seq,
        )
    return plan


def _options_from_config(scope: str, run_row: dict, config: dict) -> BuildOptions:
    """Reconstruct the build options a resume needs from the persisted run.

    Only the fields that affect stage rendering and the pipeline shape matter:
    scope, the coverage gate, the coverage threshold, and serial vs parallel.
    """
    return BuildOptions(
        scope=scope,
        skip_coverage=bool(config.get("skip_coverage")),
        coverage_threshold=int(config.get("coverage_threshold", 90)),
        sequential=(run_row.get("mode") == "serial"),
        # Story 17.1-001: carry the worker cap so a resumed parallel run fans out
        # a cohort with the same effective concurrency the original run used.
        # Defaults to 5 for runs that predate the field — matching BuildOptions.
        concurrency=int(config.get("concurrency", 5) or 5),
        # Story 14.1-001: carry the original token budget so a resume re-enforces
        # the same ceiling rather than continuing unbounded. `sdlc resume
        # --budget` overrides this in run_resume before the loop.
        budget=int(config.get("budget", 0) or 0),
        budget_policy=str(config.get("budget_policy") or "pause"),
        # Story 14.1-003: carry the rate-limit knobs so a resumed RATE_LIMITED run
        # honours the same auto-wait cap and configured window budget. Defaults
        # match BuildOptions for runs that predate these fields.
        rate_limit_max_wait_s=int(config.get("rate_limit_max_wait_s", 18000) or 18000),
        window_budget=int(config.get("window_budget", 0) or 0),
        window_s=int(config.get("window_s", 18000) or 18000),
        rate_limit_threshold=float(config.get("rate_limit_threshold", 1.0) or 1.0),
        # Story 14.2-001: carry the model-routing profile + overrides so a resumed
        # run dispatches each stage on the same model the original run chose.
        model_profile=str(config.get("model_profile") or ""),
        model_overrides=dict(config.get("model_overrides") or {}),
        # Story 14.1-002: carry the per-stage cost-estimate threshold so a resume
        # re-enforces the same gate rather than silently dispatching a stage the
        # original run gated. `sdlc resume --cost-threshold` overrides it.
        cost_estimate_threshold=int(config.get("cost_estimate_threshold", 0) or 0),
        # Story 14.1-002: carry `--auto` so a resumed run keeps the original
        # cost-gate posture — an auto run warns-and-proceeds rather than flipping
        # to interactive and wrongly gating stages it would have proceeded through.
        auto=bool(config.get("auto", False)),
        # Story 14.2-002: carry the thinking-token cap so a resumed run re-applies
        # the same MAX_THINKING_TOKENS bound. Defaults to 0 (no cap) for runs that
        # predate this field — unchanged behaviour.
        thinking_cap=int(config.get("thinking_cap", 0) or 0),
    )


def run_resume(
    scope: str,
    *,
    ledger: Ledger,
    dispatcher: Dispatcher | None = None,
    run_id: str | None = None,
    render_view: Callable[[str], None] | None = None,
    root: Path | None = None,
    registry: Registry | None = None,
    budget: int | None = None,
    budget_policy: str | None = None,
    cost_threshold: int | None = None,
    concurrency: int | None = None,
    clock: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> ResumeResult:
    """Resume the most recent interrupted run for ``scope`` from the ledger.

    Finds the run (or uses ``run_id``), recomputes the story queue from the
    markdown epics, derives each story's resume point, and re-enters the 4-stage
    loop at the exact stage each story was interrupted in — completed stories are
    never rebuilt. A run with no incomplete stories is a no-op
    (``nothing_to_resume``). Mirrors :func:`run_build`'s cohort ordering,
    dependency blocking, and close-out so the end state matches a full build.

    Story 14.1-001: the run's original token budget is carried in the ledger
    config and **re-enforced** here, so a budget-paused run does not resume
    unbounded. ``budget`` (and optionally ``budget_policy``) raises/overrides it —
    that is how a paused run is continued ("resumable once the budget is raised").
    When the accrual is already at/over the ceiling, the gate re-halts before
    dispatching anything.

    Issue #121: on terminal close-out the host-level registry entry is stamped
    with the recovered status and completed count (via the shared
    :func:`finalize_run`), so a resumed run no longer shows its stale original
    status in the dashboard sidebar. Best-effort, exactly like ``run_build``.
    """
    rid = run_id or ledger.latest_resumable_run(scope)
    if rid is None:
        return ResumeResult(run_id=None, nothing_to_resume=True)

    run_row = ledger.run_row(rid) or {}
    config = ledger.run_config(rid)
    skip_coverage = bool(config.get("skip_coverage"))
    plan = compute_resume_plan(ledger, rid, skip_coverage=skip_coverage)

    # Incomplete = not terminal AND with a stage still owed.
    incomplete = {
        sid: st
        for sid, st in plan.items()
        if st.status not in _TERMINAL_STORY_STATES and st.next_stage is not None
    }
    if not incomplete:
        return ResumeResult(run_id=rid, nothing_to_resume=True)

    opts = _options_from_config(scope, run_row, config)
    # A caller-supplied --budget raises (or overrides) the persisted ceiling; an
    # explicit --budget-policy likewise. Absent overrides keep the original.
    if budget is not None:
        opts.budget = budget
    if budget_policy is not None:
        opts.budget_policy = budget_policy
    # Story 14.1-002: --cost-threshold raises/clears the persisted estimate gate so
    # a story parked by the gate can proceed (0 disables it for this resume).
    if cost_threshold is not None:
        opts.cost_estimate_threshold = cost_threshold
    # Story 17.1-001: --concurrency overrides the persisted worker cap so a resume
    # can fan out wider/narrower than the original run (>= 1 enforced at the CLI).
    if concurrency is not None:
        opts.concurrency = concurrency

    # Story 14.2-002: bind the persisted thinking-token cap onto the real dispatch
    # seam (no-op for an injected fake / no cap), so a resumed run re-applies the
    # same MAX_THINKING_TOKENS bound the original build used. Resolved here, after
    # opts is reconstructed, because the cap lives on opts. Pass resume's own
    # ``dispatch_agent`` so a test that monkeypatches ``sdlc.resume.dispatch_agent``
    # still routes through its fake.
    dispatch = _resolve_dispatch(dispatcher, opts, dispatch_agent)

    # Issue #121: refresh the host-level registry on close-out so a resumed run no
    # longer shows its stale original status in the dashboard sidebar. A real run
    # (dispatcher is None) gets the default `Registry()` when the caller injects
    # none — the same `dispatcher is None` gate that guards reconciliation and the
    # per-story HEAD reposition, so injected-fake orchestration tests never touch
    # host state. Tests inject a path-scoped Registry to assert the refresh.
    if registry is None and dispatcher is None:
        registry = Registry()

    # Recompute the queue from the markdown source so each story carries its
    # title/epic_file/dependencies (the ledger stores progress, not the spec).
    queue = discover_queue(scope, root)
    by_id = {s.id: s for s in queue}
    run_queue: list[Story] = [by_id[sid] for sid in plan if sid in by_id]

    # Mark the run live again and announce the resume.
    ledger.run_update_status(rid, "IN_PROGRESS")
    ledger.event_log(rid, "", "info", "controller", f"run resumed: scope={scope}")
    try:  # best-effort lifecycle notification; never fail a resume
        notify("run_started", run=rid, scope=scope, mode="resume")
    except Exception:
        pass

    logs_dir = Path(f"{ledger.db_path}.logs") / rid
    cohorts = compute_cohorts(run_queue)
    # Story 11.2-007: re-record wave + intra-queue deps from the recomputed
    # cohorts so a resumed run persists the *same* parallelism structure
    # run_build would for this queue (the two scheduling paths agree).
    persist_cohort_structure(ledger, rid, cohorts)
    status: dict[str, str] = {s.id: plan[s.id].status for s in run_queue}
    resumed = 0
    budget_stopped = False
    # Story 14.1-003: re-enforce the rate-limit auto-wait/park on resume too, so a
    # RATE_LIMITED run that is resumed while the window is still closed waits or
    # re-parks just like the original build did.
    # Seed the window baseline with the accrual already on the run so the
    # reopened window measures only post-resume spend — otherwise a durably-parked
    # configured-window run would re-park forever (zero progress). See
    # _make_rate_limit_context for the rationale.
    rl_ctx = _make_rate_limit_context(
        opts, clock=clock, sleep_fn=sleep_fn,
        baseline=ledger.run_usage_totals(rid)["tokens"],
    )
    # Honour a persisted park reset time before dispatching anything: a run
    # resumed *before* its window reopens must wait (within cap) or re-park,
    # never dispatch early into a still-closed window.
    rate_limit_park: _StoryRunOutcome | None = _honor_parked_reset(
        ledger, rid, opts, rl_ctx, config.get("rate_limit_reset_at"),
    )
    cost_gated: _CostGatePause | None = None

    # Story 17.1-001: re-enter one story at its interrupted stage. The parallel
    # path isolates each story in its own worktree (concurrent agents must not
    # collide); the serial path keeps today's shared-root behaviour (workdir
    # left None) so `--sequential` resume is byte-for-byte unchanged.
    def _run_one(story: Story, *, workdir) -> _StoryRunOutcome:
        st = plan[story.id]
        return _run_story_rate_limited(
            rl_ctx, story, ledger, rid, dispatch, logs_dir,
            done_stages=st.done_pipeline_stages,
            start_attempt=st.start_attempt,
            start_escalation=st.start_escalation,
            pr_number=st.pr_number,
            bugfix_seq=st.bugfix_seq,
            workdir=workdir,
        )

    def _run_one_parallel(story: Story) -> _StoryRunOutcome:
        # Parallel resume isolates each re-entered story in its own worktree so
        # concurrent agents never collide in the shared checkout (Story 17.2-001).
        workdir = _prepare_story_workdir(
            opts, story, ledger, rid, real_run=dispatcher is None
        )
        return _run_one(story, workdir=workdir)

    workers = effective_concurrency(opts)
    for cohort in cohorts:
        if budget_stopped or rate_limit_park is not None or cost_gated is not None:
            break

        # --- Serial path (--sequential / --concurrency=1) — byte-for-byte ----
        if workers == 1:
            for story in cohort:
                st = plan[story.id]

                # Already-finished stories stay put — never rebuilt.
                if st.status in _TERMINAL_STORY_STATES:
                    continue

                # Crashed at the very end (all stages DONE, status never
                # finalised): close it out without dispatching anything.
                if st.next_stage is None:
                    status[story.id] = "DONE"
                    ledger.set_story_status(rid, story.id, "DONE")
                    ledger.event_log(
                        rid, story.id, "info", "controller",
                        "resume: all stages already complete — marked DONE",
                    )
                    resumed += 1
                    continue

                # A dependency that failed/blocked/skipped blocks this story (R2/R4).
                blocked_by = [
                    dep
                    for dep in story.dependencies
                    if status.get(dep) in {"FAILED", "BLOCKED", "SKIPPED", "AWAITING_APPROVAL"}
                ]
                if blocked_by:
                    status[story.id] = "BLOCKED"
                    ledger.set_story_status(rid, story.id, "BLOCKED")
                    ledger.event_log(
                        rid, story.id, "warn", "controller",
                        f"blocked: dependency not done ({', '.join(blocked_by)})",
                    )
                    continue

                # Story 14.1-001: re-enforce the budget ceiling before dispatching.
                # The accrual carried in the ledger already counts the pre-pause
                # spend, so an un-raised resume stops here and re-parks immediately.
                if _budget_exceeded(ledger, rid, opts.budget):
                    budget_stopped = True
                    break

                ledger.event_log(
                    rid, story.id, "info", "controller",
                    f"resume: re-entering at stage '{st.next_stage}' "
                    f"(attempt {st.start_attempt})",
                )
                try:
                    sr = _run_one(story, workdir=None)
                except _CostGatePause as gate:
                    # Story 14.1-002: the cost gate re-halted this stage on resume.
                    status[story.id] = "NEEDS_ATTENTION"
                    ledger.set_story_status(rid, story.id, "NEEDS_ATTENTION")
                    cost_gated = gate
                    break
                if sr.parked:
                    status[story.id] = "RATE_LIMITED"
                    ledger.set_story_status(rid, story.id, "RATE_LIMITED")
                    rate_limit_park = sr
                    break
                outcome = sr.status or "FAILED"
                status[story.id] = outcome
                ledger.set_story_status(rid, story.id, outcome)
                resumed += 1

                # Story 12.4-001: reposition HEAD back to the base between stories
                # so a parked story's leftover feature branch is never the base the
                # next story stacks on. Real runs only (injected fakes operate on
                # the test's cwd); best-effort and never fatal.
                if dispatcher is None:
                    _reposition_head(root or Path.cwd())
            continue

        # --- Parallel path (Story 17.1-001) — same semantics as run_build ----
        # Budget gate at the cohort boundary (the pool cannot interleave a
        # per-story check; the barrier bounds mid-cohort spend).
        if _budget_exceeded(ledger, rid, opts.budget):
            budget_stopped = True
            break

        # Resolve each story's pre-dispatch disposition sequentially (terminal
        # skip, end-crash close-out, dependency block) — only the genuinely
        # dispatchable stories are submitted to the pool.
        dispatchable: list[Story] = []
        for story in cohort:
            st = plan[story.id]
            if st.status in _TERMINAL_STORY_STATES:
                continue
            if st.next_stage is None:
                status[story.id] = "DONE"
                ledger.set_story_status(rid, story.id, "DONE")
                ledger.event_log(
                    rid, story.id, "info", "controller",
                    "resume: all stages already complete — marked DONE",
                )
                resumed += 1
                continue
            blocked_by = [
                dep
                for dep in story.dependencies
                if status.get(dep) in {"FAILED", "BLOCKED", "SKIPPED", "AWAITING_APPROVAL"}
            ]
            if blocked_by:
                status[story.id] = "BLOCKED"
                ledger.set_story_status(rid, story.id, "BLOCKED")
                ledger.event_log(
                    rid, story.id, "warn", "controller",
                    f"blocked: dependency not done ({', '.join(blocked_by)})",
                )
                continue
            ledger.event_log(
                rid, story.id, "info", "controller",
                f"resume: re-entering at stage '{st.next_stage}' "
                f"(attempt {st.start_attempt})",
            )
            dispatchable.append(story)
        if not dispatchable:
            continue

        for result in _dispatch_cohort(
            dispatchable, max_workers=workers, run_one=_run_one_parallel
        ):
            story = result.story
            if result.cost_gate is not None:
                status[story.id] = "NEEDS_ATTENTION"
                ledger.set_story_status(rid, story.id, "NEEDS_ATTENTION")
                if cost_gated is None:
                    cost_gated = result.cost_gate
                continue
            if result.error is not None:
                # Failure isolation (AC4): an unexpected raise → FAILED here; the
                # other workers already finished in the pool.
                status[story.id] = "FAILED"
                ledger.set_story_status(rid, story.id, "FAILED")
                ledger.event_log(
                    rid, story.id, "error", "controller",
                    f"story raised during concurrent resume: {result.error}",
                )
                continue
            sr = result.outcome
            assert sr is not None
            if sr.parked:
                status[story.id] = "RATE_LIMITED"
                ledger.set_story_status(rid, story.id, "RATE_LIMITED")
                if rate_limit_park is None:
                    rate_limit_park = sr
                continue
            outcome = sr.status or "FAILED"
            status[story.id] = outcome
            ledger.set_story_status(rid, story.id, outcome)
            resumed += 1

        # Reposition HEAD once after the cohort barrier (real runs only).
        if dispatcher is None:
            _reposition_head(root or Path.cwd())

    # Story 14.1-003: the resume re-hit the rate-limit window beyond the auto-wait
    # cap — durably re-park RATE_LIMITED (resumable again) without the terminal
    # close-out, exactly like run_build.
    if rate_limit_park is not None:
        assert rate_limit_park.signal is not None
        completed = sum(1 for v in status.values() if v == "DONE")
        ledger.run_update_counts(rid, completed, sum(
            1 for v in status.values() if v == "FAILED"
        ))
        reset_at = apply_rate_limit_park(
            ledger, rid, rate_limit_park.signal,
            now=rl_ctx.clock(), waited_s=rate_limit_park.waited_s,
            window_s=opts.window_s,
        )
        if render_view is not None:
            render_view(rid)
        return ResumeResult(
            run_id=rid,
            resumed=resumed,
            completed=completed,
            failed=sum(1 for v in status.values() if v == "FAILED"),
            blocked=sum(1 for v in status.values() if v == "BLOCKED"),
            needs_attention=sum(1 for v in status.values() if v == "NEEDS_ATTENTION"),
            awaiting_approval=sum(1 for v in status.values() if v == "AWAITING_APPROVAL"),
            skipped=sum(1 for v in status.values() if v == "SKIPPED"),
            nothing_to_resume=False,
            story_status=dict(status),
            rate_limited=True,
            rate_limit_reset_at=reset_at,
            rate_limit_waited_s=rate_limit_park.waited_s,
        )

    # Story 14.1-001: the budget gate re-halted the resume — apply the same
    # policy-aware stop as run_build (pause leaves IN_PROGRESS/resumable, abort
    # stamps ABORTED) and return without the normal terminal close-out.
    if budget_stopped:
        completed = sum(1 for v in status.values() if v == "DONE")
        ledger.run_update_counts(rid, completed, sum(
            1 for v in status.values() if v == "FAILED"
        ))
        usage = apply_budget_stop(ledger, rid, opts.budget, opts.budget_policy, completed)
        if render_view is not None:
            render_view(rid)
        return ResumeResult(
            run_id=rid,
            resumed=resumed,
            completed=completed,
            failed=sum(1 for v in status.values() if v == "FAILED"),
            blocked=sum(1 for v in status.values() if v == "BLOCKED"),
            needs_attention=sum(1 for v in status.values() if v == "NEEDS_ATTENTION"),
            awaiting_approval=sum(1 for v in status.values() if v == "AWAITING_APPROVAL"),
            skipped=sum(1 for v in status.values() if v == "SKIPPED"),
            nothing_to_resume=False,
            story_status=dict(status),
            budget_stopped=True,
            budget_policy=opts.budget_policy,
            accrued_tokens=usage["tokens"],
            notional_cost_usd=usage["cost_usd"],
        )

    # Story 14.1-002: the interactive cost gate re-halted the resume — leave the
    # run IN_PROGRESS (resumable) just like run_build, never a terminal close-out.
    if cost_gated is not None:
        completed = sum(1 for v in status.values() if v == "DONE")
        ledger.run_update_counts(rid, completed, sum(
            1 for v in status.values() if v == "FAILED"
        ))
        apply_cost_gate_pause(ledger, rid, opts.cost_estimate_threshold, cost_gated)
        if render_view is not None:
            render_view(rid)
        return ResumeResult(
            run_id=rid,
            resumed=resumed,
            completed=completed,
            failed=sum(1 for v in status.values() if v == "FAILED"),
            blocked=sum(1 for v in status.values() if v == "BLOCKED"),
            needs_attention=sum(1 for v in status.values() if v == "NEEDS_ATTENTION"),
            awaiting_approval=sum(1 for v in status.values() if v == "AWAITING_APPROVAL"),
            skipped=sum(1 for v in status.values() if v == "SKIPPED"),
            nothing_to_resume=False,
            story_status=dict(status),
            cost_gated=True,
        )

    # --- close out via the shared finalize helper (12.3-004) -----------------
    # The identical close-out is shared with `run_build`: finalize_run reconciles
    # against origin/main (real runs only — the dispatcher-None gate that also
    # guards the per-story HEAD reposition above), recomputes the tally, logs the
    # finish event, and stamps the run terminal, so build and resume can never
    # diverge on the terminal or the new AWAITING_APPROVAL state.
    outcome = finalize_run(
        ledger,
        rid,
        status,
        reconcile=dispatcher is None,
        root=root,
        registry=registry,
        finish_label="resume finished",
        finish_suffix=f" ({resumed} resumed)",
        render_view=render_view,
    )

    return ResumeResult(
        run_id=rid,
        resumed=resumed,
        completed=outcome.completed,
        failed=outcome.failed,
        blocked=outcome.blocked,
        needs_attention=outcome.needs_attention,
        awaiting_approval=outcome.awaiting_approval,
        skipped=outcome.skipped,
        nothing_to_resume=False,
        story_status=dict(status),
    )
