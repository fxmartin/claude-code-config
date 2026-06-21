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
    _reposition_head,
    _run_story,
    apply_budget_stop,
    finalize_run,
)
from sdlc.cohort import Story, compute_cohorts
from sdlc.discovery import discover_queue
from sdlc.dispatch import dispatch_agent

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
    intact.
    """

    story_id: str
    status: str
    done_pipeline_stages: frozenset[str]
    next_stage: str | None
    start_attempt: int
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
        else:
            start_attempt = 1

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
        # Story 14.1-001: carry the original token budget so a resume re-enforces
        # the same ceiling rather than continuing unbounded. `sdlc resume
        # --budget` overrides this in run_resume before the loop.
        budget=int(config.get("budget", 0) or 0),
        budget_policy=str(config.get("budget_policy") or "pause"),
    )


def run_resume(
    scope: str,
    *,
    ledger: Ledger,
    dispatcher: Dispatcher | None = None,
    run_id: str | None = None,
    render_view: Callable[[str], None] | None = None,
    root: Path | None = None,
    budget: int | None = None,
    budget_policy: str | None = None,
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
    """
    dispatch = dispatcher or dispatch_agent

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

    # Recompute the queue from the markdown source so each story carries its
    # title/epic_file/dependencies (the ledger stores progress, not the spec).
    queue = discover_queue(scope, root)
    by_id = {s.id: s for s in queue}
    run_queue: list[Story] = [by_id[sid] for sid in plan if sid in by_id]

    # Mark the run live again and announce the resume.
    ledger.run_update_status(rid, "IN_PROGRESS")
    ledger.event_log(rid, "", "info", "controller", f"run resumed: scope={scope}")

    logs_dir = Path(f"{ledger.db_path}.logs") / rid
    cohorts = compute_cohorts(run_queue)
    status: dict[str, str] = {s.id: plan[s.id].status for s in run_queue}
    resumed = 0
    budget_stopped = False

    for cohort in cohorts:
        if budget_stopped:
            break
        for story in cohort:
            st = plan[story.id]

            # Already-finished stories stay put — never rebuilt.
            if st.status in _TERMINAL_STORY_STATES:
                continue

            # Crashed at the very end (all stages DONE, status never finalised):
            # close it out without dispatching anything.
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
            outcome = _run_story(
                story, opts, ledger, rid, dispatch, logs_dir,
                done_stages=st.done_pipeline_stages,
                start_attempt=st.start_attempt,
                pr_number=st.pr_number,
                bugfix_seq=st.bugfix_seq,
            )
            status[story.id] = outcome
            ledger.set_story_status(rid, story.id, outcome)
            resumed += 1

            # Story 12.4-001: reposition HEAD back to the base between stories so
            # a parked story's leftover feature branch is never the base the next
            # story stacks on. Real runs only (injected fakes operate on the
            # test's cwd); best-effort and never fatal.
            if dispatcher is None:
                _reposition_head(root or Path.cwd())

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
