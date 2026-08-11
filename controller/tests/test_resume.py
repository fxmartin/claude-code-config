# ABOUTME: Tests for controller-native resume (Story 10.1-001).
# ABOUTME: Seeds an interrupted ledger via fixtures, asserts resume re-enters right.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    Ledger,
    _parse_harness_routing_event,
    run_build,
)
from sdlc.cohort import Story
from sdlc.discovery import discover_queue
from sdlc.registry import Registry, RunRecord
import sdlc.resume as resume_mod
from sdlc.resume import ResumeResult, compute_resume_plan, run_resume

from test_build import (  # reuse the canned dispatchers
    FakeDispatcher,
    _RaisingDispatcher,
)

# A two-story epic-99 project, coverage gate off so the pipeline is
# build -> review -> merge (keeps the fixtures small and explicit).
_SAMPLE_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 99.1-002: Two
**Priority**: P2
**Points**: 2
**Dependencies**: Story 99.1-001.
"""


def _make_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def _seed_interrupted(db_path: Path) -> str:
    """A run interrupted mid-review on story 99.1-002.

    99.1-001: build+review+merge DONE, PR #100, story DONE.
    99.1-002: build DONE, review IN_PROGRESS (crash), story IN_PROGRESS, PR #100.
    Run is left IN_PROGRESS (a clean close-out never happened). skip_coverage.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "controller", "run started: scope=epic-99 mode=serial")
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True, "coverage_threshold": 90}))

    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO")

    # 99.1-001 fully done.
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "DONE")

    # 99.1-002: build done, review interrupted.
    ledger.stage_start(run_id, "99.1-002", "build", 1)
    ledger.stage_finish(run_id, "99.1-002", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-002", 100)
    ledger.stage_start(run_id, "99.1-002", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")
    return run_id


def _seed_complete(db_path: Path) -> str:
    """A run where every story is DONE — nothing to resume."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_status(run_id, "99.1-001", "DONE")
    ledger.run_update_status(run_id, "DONE")
    return run_id


# --- resume plan -----------------------------------------------------------


def test_compute_resume_plan_identifies_next_stage(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    plan = compute_resume_plan(Ledger(db), Ledger(db).latest_run_id(), skip_coverage=True)

    done = plan["99.1-001"]
    assert done.status == "DONE"
    assert done.next_stage is None  # nothing left to run

    interrupted = plan["99.1-002"]
    assert interrupted.status == "IN_PROGRESS"
    assert "build" in interrupted.done_pipeline_stages
    assert interrupted.next_stage == "review"  # re-enter at the interrupted stage
    assert interrupted.start_attempt == 2  # continues counting past the crashed attempt
    assert interrupted.pr_number == 100  # PR number preserved


def test_resume_bugfix_seq_continues_past_reask_rows(tmp_path: Path) -> None:
    """A prior envelope re-ask must advance the resumed monotonic seq (12.1-001).

    The 'reask' and 'bugfix' stages share the ``bugfix_seq`` counter for their
    attempt number. A re-ask that *succeeded* leaves a 'reask' row but no
    'bugfix' row; if resume reconstructs ``bugfix_seq`` from 'bugfix' rows only,
    the next re-ask reuses an existing attempt and hits the stages PRIMARY KEY.
    Resume must continue past the highest of both.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    # Build recovered via an envelope re-ask (reask seq=1, no bugfix row), then
    # the run crashed mid-review.
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "reask", 1)
    ledger.stage_finish(run_id, "99.1-001", "reask", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=True)
    # The resumed seq must be at least the existing reask attempt so the next
    # recovery row cannot collide on (run_id, story_id, 'reask', seq).
    assert plan["99.1-001"].bugfix_seq >= 1


def test_resume_bugfix_seq_continues_past_commitlint_rows(tmp_path: Path) -> None:
    """A prior commitlint re-ask must advance the resumed monotonic seq (12.2-002).

    The 'commitlint' stage shares the ``bugfix_seq`` counter with 'bugfix' and
    'reask' (Story 12.2-002). A build commit that needed a commitlint amend
    leaves a 'commitlint' row but no 'bugfix'/'reask' row; if resume rebuilds
    ``bugfix_seq`` from those two names only, a later commit-authoring stage that
    also needs a commitlint amend reuses attempt 1 and collides on the stages
    PRIMARY KEY (run_id, story_id, 'commitlint', attempt). Resume must continue
    past the highest commitlint attempt too.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": False}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    # Build committed, then its message needed a commitlint amend (commitlint
    # seq=1, no bugfix/reask row), then the run crashed mid-coverage.
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "commitlint", 1)
    ledger.stage_finish(run_id, "99.1-001", "commitlint", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "coverage", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=False)
    # The resumed seq must be at least the existing commitlint attempt so the
    # next commitlint row cannot collide on the stages PRIMARY KEY.
    assert plan["99.1-001"].bugfix_seq >= 1


def test_resume_escalation_reflects_prior_failed_attempts(tmp_path: Path) -> None:
    """Cheap-first escalation resumes on the tier the stage had climbed to (14.2-003).

    A stage that failed twice before a crash had escalated two tiers; resume must
    reconstruct that level from its FAILED-attempt count so it does not drop back
    to the cheap base. A crashed (IN_PROGRESS) attempt never escalated, so it must
    not be counted.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    # build failed twice (two cheap-first tier bumps), then crashed mid third try.
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "FAILED")
    ledger.stage_start(run_id, "99.1-001", "build", 2)
    ledger.stage_finish(run_id, "99.1-001", "build", 2, "FAILED")
    ledger.stage_start(run_id, "99.1-001", "build", 3)  # left IN_PROGRESS (crashed)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=True)
    st = plan["99.1-001"]
    assert st.next_stage == "build"
    assert st.start_attempt == 4  # past the crashed attempt 3
    assert st.start_escalation == 2  # two FAILED attempts → two prior tier bumps


def test_resume_treats_docs_only_skipped_coverage_as_done(tmp_path: Path) -> None:
    """A docs-only coverage skip is terminal — resume must not re-enter it (27.2-001).

    The docs-only gate records coverage SKIPPED/docs-only and continues to
    review. If resume counts only DONE rows, an interruption past the skip
    (crash or rate-limit park mid-review) re-plans from coverage; review then
    restarts at attempt 1 in `_run_story` (non-first pending stages always do)
    and its `stage_start` INSERT collides with the existing review attempt-1
    row on the stages PRIMARY KEY, wedging every subsequent resume. The
    deterministic verdict makes the skip as final as a DONE, so resume must
    plan straight from the interrupted review at the next attempt.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": False}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "coverage", 1)
    ledger.stage_finish(run_id, "99.1-001", "coverage", 1, "SKIPPED", "docs-only")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.stage_start(run_id, "99.1-001", "review", 1)  # left IN_PROGRESS (crashed)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=False)
    st = plan["99.1-001"]
    assert "coverage" in st.done_pipeline_stages
    assert st.next_stage == "review"
    assert st.start_attempt == 2  # past the crashed review attempt, no PK collision


def test_resume_treats_precheck_skipped_coverage_as_done(tmp_path: Path) -> None:
    """A coverage-pre-check skip is terminal, exactly like docs-only (27.3-001).

    The deterministic pre-check (tests green + changed-file coverage >=
    threshold) records coverage SKIPPED/coverage-pre-check and continues to
    review; resume must plan straight from an interrupted review rather than
    re-entering the skipped stage and colliding on the stages PRIMARY KEY.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": False}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "coverage", 1)
    ledger.stage_finish(run_id, "99.1-001", "coverage", 1, "SKIPPED", "coverage-pre-check")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.stage_start(run_id, "99.1-001", "review", 1)  # left IN_PROGRESS (crashed)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=False)
    st = plan["99.1-001"]
    assert "coverage" in st.done_pipeline_stages
    assert st.next_stage == "review"
    assert st.start_attempt == 2


def test_resume_still_reenters_cost_gated_skipped_stage(tmp_path: Path) -> None:
    """A cost-gate SKIPPED stage keeps its pause semantics — resume re-runs it.

    Only the docs-only skip is terminal (deterministic verdict); the cost gate
    (14.1-002) skips a stage to pause the run *at* that stage so `sdlc resume
    --cost-threshold` can raise the gate and dispatch it. Keying done-ness off
    failure_category keeps the two apart.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": False}))
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-001", "coverage", 1)
    ledger.stage_finish(run_id, "99.1-001", "coverage", 1, "SKIPPED", "cost-gate")
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")

    plan = compute_resume_plan(ledger, run_id, skip_coverage=False)
    st = plan["99.1-001"]
    assert st.next_stage == "coverage"  # the gated stage itself is re-entered
    assert st.start_attempt == 2  # continuing past the gated attempt row


def test_resume_escalation_zero_when_stage_never_failed(tmp_path: Path) -> None:
    """A stage interrupted on its first (never-failed) attempt resumes cheap."""
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)  # 99.1-002 crashed mid-review on attempt 1, no FAILED rows
    plan = compute_resume_plan(Ledger(db), Ledger(db).latest_run_id(), skip_coverage=True)
    assert plan["99.1-002"].start_escalation == 0


def test_latest_resumable_run_finds_in_progress(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)
    assert Ledger(db).latest_resumable_run("epic-99") == run_id
    # A completed run is not resumable.
    db2 = tmp_path / "done.db"
    _seed_complete(db2)
    assert Ledger(db2).latest_resumable_run("epic-99") is None


# --- run_resume ------------------------------------------------------------


def test_resume_continues_from_interrupted_stage(tmp_path: Path) -> None:
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path
    )

    assert isinstance(result, ResumeResult)
    assert result.nothing_to_resume is False
    assert result.completed == 2  # both stories end DONE
    assert result.failed == 0
    assert result.resumed == 1  # only 99.1-002 was re-run

    # Completed story is never rebuilt; the interrupted story resumes at review.
    assert ("build", "99.1-001") not in dispatcher.calls
    assert ("review", "99.1-001") not in dispatcher.calls
    assert ("build", "99.1-002") not in dispatcher.calls  # build was already DONE
    assert ("review", "99.1-002") in dispatcher.calls
    assert ("merge", "99.1-002") in dispatcher.calls

    # Ledger reflects the completed run.
    ledger = Ledger(db)
    rows = {r["story_id"]: r for r in ledger.story_rows(ledger.latest_run_id())}
    assert rows["99.1-002"]["status"] == "DONE"


def test_resume_no_incomplete_is_noop(tmp_path: Path) -> None:
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_complete(db)
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path)
    assert result.nothing_to_resume is True


def test_resume_no_run_at_all_is_noop(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path)
    assert result.nothing_to_resume is True
    assert result.run_id is None


# --- live-owner guard (issue #595) ------------------------------------------
#
# Two processes (`sdlc resume` and/or `sdlc fix`) must never drive the same run
# concurrently — the incident that motivated this guard left a real ledger with
# a `1 done` finish record immediately followed by `0 done, 1 failed` for the
# same run, because both writers raced to the finish line.

# pid 1 is always alive (init) but never this test process's own pid, so it
# stands in for "some other live process already owns this run" without having
# to actually fork one — `pid_alive(1)` is True either as root (the kill
# succeeds directly) or not (PermissionError also counts as alive).
_OTHER_LIVE_PID = 1


def _seed_registry_live(tmp_path: Path, db: Path, run_id: str, scope: str) -> Registry:
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(
            run_id=run_id,
            repo=str(tmp_path.resolve()),
            db=str(db.resolve()),
            scope=scope,
            pid=_OTHER_LIVE_PID,
            status="IN_PROGRESS",
            started_at="2026-08-11T09:00:00+00:00",
        )
    )
    return registry


def test_run_resume_refuses_when_a_live_owner_holds_the_run(tmp_path: Path) -> None:
    """The issue #595 regression: a second resume of the same run refuses."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)
    registry = _seed_registry_live(tmp_path, db, run_id, "epic-99")
    dispatcher = FakeDispatcher()

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        registry=registry,
    )

    assert result.refused is True
    assert result.nothing_to_resume is False
    assert run_id in result.refusal_reason
    assert str(_OTHER_LIVE_PID) in result.refusal_reason
    assert f"sdlc resume --run {run_id} --force" in result.refusal_reason
    # No ledger mutation and no dispatch — the refusal fires before either.
    assert dispatcher.calls == []
    assert (Ledger(db).run_row(run_id) or {}).get("status") != "DONE"


def test_run_resume_ignores_a_dead_owner(tmp_path: Path) -> None:
    """A crashed prior resume (pid gone) is reclaimable, not a collision."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(
            run_id=run_id,
            repo=str(tmp_path.resolve()),
            db=str(db.resolve()),
            scope="epic-99",
            pid=2**31 - 1,  # essentially never a real process
            status="IN_PROGRESS",
            started_at="2026-08-11T09:00:00+00:00",
        )
    )

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        registry=registry,
    )
    assert result.refused is False
    assert result.completed == 2


def test_run_resume_force_overrides_a_live_owner(tmp_path: Path) -> None:
    """`--force` (issue #595) is the documented "only if that pid is gone" override."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)
    registry = _seed_registry_live(tmp_path, db, run_id, "epic-99")

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        registry=registry, force=True,
    )
    assert result.refused is False
    assert result.completed == 2


# --- behaviour parity ------------------------------------------------------


def test_resume_reaches_same_end_state_as_full_build(tmp_path: Path) -> None:
    """Parity: resuming an interrupted run reaches the same end state a full
    build would — both leave every story DONE with a merge stage recorded."""
    _make_project(tmp_path)

    # Reference: a clean full build of the same scope.
    ref_db = tmp_path / "ref.db"
    queue = [
        Story("99.1-001", "One", "99", "sample", "epic-99.md", "P1", 1, "general-purpose", []),
        Story("99.1-002", "Two", "99", "sample", "epic-99.md", "P2", 2, "general-purpose", ["99.1-001"]),
    ]
    from sdlc.build import BuildOptions

    full = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=queue,
        ledger=Ledger(ref_db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )

    # Resumed: an interrupted run finished via resume.
    res_db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(res_db)
    resumed = run_resume("epic-99", ledger=Ledger(res_db), dispatcher=FakeDispatcher(), root=tmp_path)

    assert resumed.completed == full.completed
    assert resumed.failed == full.failed

    def _final_statuses(db: Path) -> dict[str, str]:
        led = Ledger(db)
        return {r["story_id"]: r["status"] for r in led.story_rows(led.latest_run_id())}

    assert _final_statuses(res_db) == _final_statuses(ref_db)


# --- edge-case resume paths ------------------------------------------------


def _seed_single_interrupted_at_review(db_path: Path) -> str:
    """A single-story run interrupted mid-review on 99.1-001.

    build DONE, review IN_PROGRESS (crash), story IN_PROGRESS, PR #100, run
    left IN_PROGRESS. The story has no dependencies so it never blocks.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.stage_start(run_id, "99.1-001", "build", 1)
    ledger.stage_finish(run_id, "99.1-001", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.stage_start(run_id, "99.1-001", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")
    return run_id


def _seed_all_stages_done_unfinalised(db_path: Path) -> str:
    """A run left IN_PROGRESS where the only story has every stage DONE but its
    status was never finalised (crash between the last stage and close-out)."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")  # never finalised
    return run_id


def _seed_one_unfinalised_one_incomplete(db_path: Path) -> str:
    """99.1-001: all stages DONE but status IN_PROGRESS (unfinalised end).
    99.1-002: build DONE, review interrupted (genuinely incomplete)."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO")
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")  # never finalised
    ledger.stage_start(run_id, "99.1-002", "build", 1)
    ledger.stage_finish(run_id, "99.1-002", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-002", 100)
    ledger.stage_start(run_id, "99.1-002", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")
    return run_id


def _seed_skipped_dep_blocks(db_path: Path) -> str:
    """99.1-001 SKIPPED; 99.1-002 (depends on it) is build-done, review-interrupted.
    The skipped dependency must block 99.1-002 when the run is resumed. (A FAILED
    dependency is *retried* on resume — only DONE/SKIPPED stay terminal — so the
    block path needs a terminal-but-unsuccessful dependency: SKIPPED.)"""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO")
    ledger.set_story_status(run_id, "99.1-001", "SKIPPED")
    ledger.stage_start(run_id, "99.1-002", "build", 1)
    ledger.stage_finish(run_id, "99.1-002", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-002", 100)
    ledger.stage_start(run_id, "99.1-002", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")
    return run_id


# Issue #536: an epic whose first stories the markdown already marks shipped.
# run_build records those as SKIPPED for the audit trail only — they never enter
# the cohorts or the status map, so a dependent of theirs is *satisfied*, not
# blocked. Resume has to reconstruct the same partition from the same signal.
_DONE_DEP_EPIC = """# Epic 98

##### Story 98.1-001: Shipped one
**Priority**: P1
**Points**: 1
**Status**: Done
**Dependencies**: None.

##### Story 98.2-001: Shipped two
**Priority**: P1
**Points**: 1
**Status**: Done
**Dependencies**: Story 98.1-001.

##### Story 98.3-001: Unfinished
**Priority**: P1
**Points**: 2
**Dependencies**: Story 98.1-001, Story 98.2-001.
"""


def _make_done_dep_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    (stories / "epic-98-shipped.md").write_text(_DONE_DEP_EPIC, encoding="utf-8")
    return tmp_path


def _seed_done_dep_parked(db_path: Path, *, mode: str = "serial") -> str:
    """The Issue #536 shape: the two deps were already Done in the epic when the
    build started (persisted SKIPPED for audit, exactly as run_build's pre-loop
    does — no stages, never dispatched), and the dependent parked mid-review with
    an open PR. Resume must re-enter it, not cascade-block on the shipped deps."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-98", mode)
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({"skip_coverage": True, "concurrency": 3}),
    )
    for sid, title in (("98.1-001", "Shipped one"), ("98.2-001", "Shipped two")):
        ledger.story_upsert(
            run_id, sid, "98", title, "P1", 1, "general-purpose", "", None, "SKIPPED",
        )
    ledger.story_upsert(
        run_id, "98.3-001", "98", "Unfinished", "P1", 2,
        "general-purpose", "", None, "TODO",
    )
    ledger.stage_start(run_id, "98.3-001", "build", 1)
    ledger.stage_finish(run_id, "98.3-001", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "98.3-001", 1)
    ledger.stage_start(run_id, "98.3-001", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "98.3-001", "NEEDS_ATTENTION")
    return run_id


def test_resume_dispatches_story_whose_deps_were_done_in_epic(tmp_path: Path) -> None:
    """Issue #536: dependencies the epic already marked ``Status: Done`` are
    recorded SKIPPED for the audit trail, not because their work failed — they
    must not block the unfinished story that depends on them. `sdlc build` sends
    the operator here, so resume returning ``0 resumed`` strands the epic."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_done_dep_parked(db)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert ("review", "98.3-001") in dispatcher.calls  # actually re-entered
    assert result.resumed == 1
    assert result.blocked == 0
    assert result.story_status["98.3-001"] == "DONE"
    rows = {r["story_id"]: r for r in Ledger(db).story_rows(rid)}
    assert rows["98.3-001"]["status"] == "DONE"
    # The shipped stories stay SKIPPED in the ledger and are still tallied as
    # skipped in the summary (counted outside `status`, via `extra_skipped`).
    assert rows["98.1-001"]["status"] == "SKIPPED"
    assert rows["98.2-001"]["status"] == "SKIPPED"
    assert result.skipped == 2
    assert result.completed == 1
    # They are not part of this resume's schedule, so they carry no status entry.
    assert "98.1-001" not in result.story_status
    assert "98.2-001" not in result.story_status


def test_resume_parallel_dispatches_story_whose_deps_were_done_in_epic(
    tmp_path: Path,
) -> None:
    """Issue #536 on the parallel path: `_triage`'s hold check treats any
    ``status[dep] != "DONE"`` as unresolved, so a shipped dep left in the status
    map would hold the dependent forever rather than merely block it."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_done_dep_parked(db, mode="parallel")
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert ("review", "98.3-001") in dispatcher.calls
    assert result.resumed == 1
    assert result.blocked == 0
    assert result.skipped == 2
    assert result.story_status["98.3-001"] == "DONE"


# Issue #536 coverage gate: the three early-return tallies (rate-limit park,
# budget stop, cost gate) each fold `len(done_skip_ids)` into `skipped` — none
# of the pre-existing budget/rate-limit/cost-gate suites exercise a run whose
# ledger also carries shipped (done-in-epic) dependencies, so the fold itself
# was never exercised with a non-zero count.


def _seed_done_dep_budget_stopped(db_path: Path) -> str:
    """Same shipped-deps shape as `_seed_done_dep_parked`, but the completed
    build stage already carries enough accrued tokens to trip a tiny budget
    ceiling before the unfinished story's review stage is re-entered —
    exercising the budget-stop early return's `len(done_skip_ids)` fold. (A
    fresh `run_build` cannot reproduce this: the budget gate there is checked
    once per *story*, so a single-story queue always finishes its first story
    before the ceiling is ever re-read.)"""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-98", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({"skip_coverage": True, "budget": 1}),
    )
    for sid, title in (("98.1-001", "Shipped one"), ("98.2-001", "Shipped two")):
        ledger.story_upsert(
            run_id, sid, "98", title, "P1", 1, "general-purpose", "", None, "SKIPPED",
        )
    ledger.story_upsert(
        run_id, "98.3-001", "98", "Unfinished", "P1", 2,
        "general-purpose", "", None, "TODO",
    )
    ledger.stage_start(run_id, "98.3-001", "build", 1)
    ledger.stage_finish(run_id, "98.3-001", "build", 1, "DONE")
    ledger.stage_set_usage(
        run_id, "98.3-001", "build", 1,
        session_id=None, input_tokens=100, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0, cost_usd=None,
    )
    ledger.set_story_pr(run_id, "98.3-001", 1)
    return run_id


def test_resume_budget_stop_tallies_done_deps_as_skipped(tmp_path: Path) -> None:
    """Issue #536: the budget-stop early return folds `len(done_skip_ids)` into
    `skipped` exactly like the normal close-out — the ceiling is already tripped
    before the unfinished story dispatches, but its shipped deps must still be
    counted rather than silently dropped."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_done_dep_budget_stopped(db)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert result.budget_stopped is True
    assert result.resumed == 0
    assert dispatcher.calls == []
    assert result.skipped == 2
    assert "98.1-001" not in result.story_status
    assert "98.2-001" not in result.story_status


def _build_done_dep_cost_gated(tmp_path: Path):
    """Build epic-98 (two shipped deps + one unfinished story) interactively with
    a trivially-low cost-estimate threshold so the unfinished story's first stage
    gates before ever dispatching — mirrors `_build_cost_gated` in
    test_cost_estimate.py, seeded with Issue #536's shipped-deps shape."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-98", tmp_path)
    assert len(queue) == 3
    opts = BuildOptions(
        scope="epic-98", skip_preflight=True, sequential=True,
        skip_coverage=True, auto=False, cost_estimate_threshold=1,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.cost_gated is True
    assert result.skipped == 2
    return db, result


def test_resume_cost_gate_tallies_done_deps_as_skipped(tmp_path: Path) -> None:
    """Issue #536: the interactive cost-gate early return must fold
    `len(done_skip_ids)` into `skipped` too — an un-raised resume re-gates before
    the unfinished story dispatches, but its shipped deps must still be counted."""
    db, result = _build_done_dep_cost_gated(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert resumed.run_id == result.run_id
    assert resumed.cost_gated is True
    assert dispatcher.calls == []
    assert resumed.skipped == 2
    assert "98.1-001" not in resumed.story_status
    assert "98.2-001" not in resumed.story_status


def _seed_done_dep_rate_limit_parked(db_path: Path) -> str:
    """Same shipped-deps shape as `_seed_done_dep_parked`, but the ledger already
    carries a persisted `rate_limit_reset_at` far beyond the auto-wait cap, so
    resume must durably re-park before ever touching the unfinished story —
    exercising the rate-limit early return's `len(done_skip_ids)` fold."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-98", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({
            "skip_coverage": True,
            "rate_limit_reset_at": 999_999.0,
            "rate_limit_max_wait_s": 300,
        }),
    )
    for sid, title in (("98.1-001", "Shipped one"), ("98.2-001", "Shipped two")):
        ledger.story_upsert(
            run_id, sid, "98", title, "P1", 1, "general-purpose", "", None, "SKIPPED",
        )
    ledger.story_upsert(
        run_id, "98.3-001", "98", "Unfinished", "P1", 2,
        "general-purpose", "", None, "TODO",
    )
    return run_id


def test_resume_rate_limit_park_tallies_done_deps_as_skipped(tmp_path: Path) -> None:
    """Issue #536: the rate-limit re-park early return must fold
    `len(done_skip_ids)` into `skipped` too — the persisted park short-circuits
    before the unfinished story is ever touched, but its shipped deps must still
    be counted."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_done_dep_rate_limit_parked(db)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        clock=lambda: 0.0, sleep_fn=lambda _s: None,
    )

    assert result.rate_limited is True
    assert result.resumed == 0
    assert dispatcher.calls == []
    assert result.skipped == 2
    assert "98.1-001" not in result.story_status
    assert "98.2-001" not in result.story_status


def _seed_done_dep_parked_with_orphan_row(db_path: Path, *, mode: str = "serial") -> str:
    """Same shape as `_seed_done_dep_parked`, plus one extra ledger row
    (`98.0-000`) for a story id that no longer exists in the epic markdown at
    all — e.g. renumbered/removed between the interrupted build and the resume.
    The `by_id.get(sid) is not None` guard in `done_skip_ids` must keep this
    orphan from raising or being folded into the shipped-dep count."""
    run_id = _seed_done_dep_parked(db_path, mode=mode)
    ledger = Ledger(db_path)
    ledger.story_upsert(
        run_id, "98.0-000", "98", "Orphan", "P1", 1,
        "general-purpose", "", None, "SKIPPED",
    )
    return run_id


def test_resume_ignores_orphan_ledger_row_not_in_epic(tmp_path: Path) -> None:
    """Issue #536: a ledger row for a story id no longer present in the epic
    markdown (renamed/removed) must not crash or be folded into `done_skip_ids`
    — `by_id.get(sid)` is None for it, so the guard must simply drop it, leaving
    the genuinely shipped deps' count (2) unaffected."""
    _make_done_dep_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_done_dep_parked_with_orphan_row(db)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-98", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert ("review", "98.3-001") in dispatcher.calls
    assert result.resumed == 1
    # Only the two genuinely shipped deps are folded into `skipped` — the
    # orphan row is neither dispatched nor double-counted.
    assert result.skipped == 2
    assert "98.0-000" not in result.story_status


def test_resume_all_stages_done_unfinalised_closes_out(tmp_path: Path) -> None:
    """A resumable run whose only story has every stage DONE (just not finalised)
    has no stage to dispatch, but it is *not* a no-op: leaving it stranded
    IN_PROGRESS would also strand its per-story worktree (Story 17.2-002). The
    end-crash story is closed out (marked DONE, no dispatch) so the run finalises
    coherently and its worktree can be torn down rather than leaked."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_all_stages_done_unfinalised(db)
    dispatcher = FakeDispatcher()
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)
    assert result.nothing_to_resume is False
    assert result.run_id == rid
    assert dispatcher.calls == []  # closed out without dispatching any stage
    rows = {r["story_id"]: r for r in Ledger(db).story_rows(rid)}
    assert rows["99.1-001"]["status"] == "DONE"


def test_resume_closes_out_unfinalised_story_without_dispatch(tmp_path: Path) -> None:
    """When a run still has incomplete work, an all-stages-done-but-unfinalised
    story is closed out (marked DONE) without re-dispatching any stage."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_one_unfinalised_one_incomplete(db)
    dispatcher = FakeDispatcher()
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    # 99.1-001 was closed out with no dispatch of any of its stages.
    assert ("build", "99.1-001") not in dispatcher.calls
    assert ("review", "99.1-001") not in dispatcher.calls
    assert ("merge", "99.1-001") not in dispatcher.calls
    # 99.1-002 genuinely resumed at review.
    assert ("review", "99.1-002") in dispatcher.calls

    ledger = Ledger(db)
    rows = {r["story_id"]: r for r in ledger.story_rows(ledger.latest_run_id())}
    assert rows["99.1-001"]["status"] == "DONE"
    assert rows["99.1-002"]["status"] == "DONE"
    assert result.completed == 2


def test_resume_blocks_story_with_skipped_dependency(tmp_path: Path) -> None:
    """A story whose dependency is SKIPPED is blocked on resume (R2/R4); the run
    closes out FAILED and the render-view hook is invoked with the run id."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_skipped_dep_blocks(db)
    dispatcher = FakeDispatcher()
    rendered: list[str] = []
    result = run_resume(
        "epic-99",
        ledger=Ledger(db),
        dispatcher=dispatcher,
        root=tmp_path,
        render_view=rendered.append,
    )

    assert result.blocked == 1
    assert result.story_status["99.1-002"] == "BLOCKED"
    # The blocked story is never dispatched.
    assert ("review", "99.1-002") not in dispatcher.calls
    # render_view was called once with the resumed run id.
    assert rendered == [rid]

    ledger = Ledger(db)
    assert ledger.run_row(ledger.latest_run_id())["status"] == "FAILED"


def test_resume_marks_needs_attention_when_committed_but_unparseable(
    tmp_path: Path, monkeypatch
) -> None:
    """Resuming a stage whose agent emits an unparseable result, while a story
    commit already exists, attempts bounded recovery (envelope re-ask + bugfix)
    and — once exhausted — preserves the work as NEEDS_ATTENTION (R10), closing
    the run out NEEDS_ATTENTION (Story 12.1-001)."""
    _make_project(tmp_path)
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    db = tmp_path / ".sdlc-state.db"
    _seed_single_interrupted_at_review(db)
    disp = _RaisingDispatcher(raise_on="review")
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=disp, root=tmp_path)

    assert result.needs_attention == 1
    assert result.failed == 0
    assert result.blocked == 0
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    # Recovery is attempted before parking, but the committed work is never
    # discarded — it ends NEEDS_ATTENTION, not FAILED (R10).
    assert any(agent == "bugfix" for agent, _ in disp.calls)

    ledger = Ledger(db)
    assert ledger.run_row(ledger.latest_run_id())["status"] == "NEEDS_ATTENTION"


def test_resume_high_risk_merge_block_parks_awaiting_approval(tmp_path: Path) -> None:
    """Resuming into a high-risk-blocked merge parks AWAITING_APPROVAL (12.3-003).

    The run terminal is AWAITING_APPROVAL — never FAILED — and no bugfix agent
    is dispatched (the block cannot be self-approved).
    """
    from test_build import _high_risk_merge_block

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_single_interrupted_at_review(db)
    dispatcher = FakeDispatcher(
        overrides={("merge", "99.1-001"): _high_risk_merge_block()}
    )
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    assert result.story_status["99.1-001"] == "AWAITING_APPROVAL"
    assert result.awaiting_approval == 1
    assert result.failed == 0
    assert not any(a == "bugfix" for a, _ in dispatcher.calls)
    assert Ledger(db).run_row(rid)["status"] == "AWAITING_APPROVAL"


def test_resume_ci_gate_only_block_parks_awaiting_approval(
    tmp_path: Path, monkeypatch
) -> None:
    """Story 25.1-001: the epic-23 run-0541804d regression, resume path.

    On resume the gate check is already concluded red, so the merge CI gate
    (23.2-002) blocks *before* the merge agent can report BLOCKED_HIGH_RISK.
    The deterministic CR re-check must park the story AWAITING_APPROVAL —
    byte-for-byte the build path — never burn the bugfix loop into FAILED.
    """
    from sdlc import build_issue
    from sdlc import issue_host as ih

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_single_interrupted_at_review(db)
    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_FAILED)
    monkeypatch.setattr(
        build_issue, "change_request_checks",
        lambda *a, **k: ih.ChangeRequestChecks(
            labels=("risk:high",),
            checks=(
                ("High-risk file approval gate", ih.CR_FAILED),
                ("tests", ih.CR_SUCCESS),
            ),
        ),
    )
    dispatcher = FakeDispatcher()
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    assert result.story_status["99.1-001"] == "AWAITING_APPROVAL"
    assert result.awaiting_approval == 1
    assert result.failed == 0
    # The merge agent was never dispatched (the CI gate blocked pre-dispatch)
    # and the bugfix loop never ran (it cannot self-approve).
    assert ("merge", "99.1-001") not in dispatcher.calls
    assert not any(a == "bugfix" for a, _ in dispatcher.calls)
    assert Ledger(db).run_row(rid)["status"] == "AWAITING_APPROVAL"


def test_resume_real_run_repositions_head_after_each_story(
    tmp_path: Path, monkeypatch
) -> None:
    """On a real run (``dispatcher=None``), resume repositions HEAD between
    stories (Story 12.4-001) so a parked story's leftover ``feature/<id>`` branch
    is never the base the next story stacks on.

    ``dispatcher=None`` selects the module-level ``dispatch_agent``; route it
    through a fake so no subprocess agents spawn, and spy on ``_reposition_head``
    (neutralizing its git side effect on the live checkout) to prove the
    real-run branch fires exactly for the story that genuinely resumed.
    """
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)  # only 99.1-002 resumes (at review); 99.1-001 is DONE

    monkeypatch.setattr("sdlc.resume.dispatch_agent", FakeDispatcher())

    reposition_calls: list[Path] = []
    monkeypatch.setattr(
        "sdlc.resume._reposition_head",
        lambda root: reposition_calls.append(root),
    )

    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=None, root=tmp_path)

    assert result.completed == 2
    assert result.failed == 0
    assert result.resumed == 1  # only 99.1-002 was re-run
    # HEAD repositioned once — for the single story that actually resumed. The
    # already-DONE 99.1-001 closes out via the early ``continue`` and never
    # reaches the reposition call.
    assert reposition_calls == [tmp_path]


def test_resume_injected_dispatcher_never_repositions_head(
    tmp_path: Path, monkeypatch
) -> None:
    """With an injected dispatcher (the controller's own orchestration tests),
    resume must NOT touch the real checkout — ``_reposition_head`` is guarded
    behind ``dispatcher is None`` (Story 12.4-001)."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    reposition_calls: list[Path] = []
    monkeypatch.setattr(
        "sdlc.resume._reposition_head",
        lambda root: reposition_calls.append(root),
    )

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path
    )

    assert result.resumed == 1
    assert reposition_calls == []  # injected fake → no git side effect


# --- registry finalize on resume (#121) ------------------------------------


def _seed_registry_failed(reg_path: Path, run_id: str, db_path: Path) -> Registry:
    """Seed a registry entry for ``run_id`` stamped terminal FAILED.

    Mirrors the host-level state the dashboard sidebar reads after a build that
    finished FAILED — the stale status a later resume must overwrite.
    """
    registry = Registry(reg_path)
    registry.register(
        RunRecord(
            run_id=run_id,
            repo=str(db_path.parent.resolve()),
            db=str(db_path.resolve()),
            scope="epic-99",
            pid=1,
            status="IN_PROGRESS",
            started_at="",
            total=2,
            completed=1,
        )
    )
    registry.mark_finished(run_id, "FAILED", completed=1)
    return registry


def test_resume_refreshes_registry_to_done(tmp_path: Path) -> None:
    """A run recovered via resume must overwrite its stale registry status so the
    dashboard sidebar no longer shows the original FAILED (#121)."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)

    reg_path = tmp_path / "registry.json"
    registry = _seed_registry_failed(reg_path, run_id, db)
    assert registry.records()[0].status == "FAILED"

    result = run_resume(
        "epic-99",
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        root=tmp_path,
        registry=registry,
    )

    assert result.completed == 2
    record = next(r for r in registry.records() if r.run_id == run_id)
    assert record.status == "DONE"
    assert record.completed == 2  # the recovered DONE count, not the stale 1


def test_resume_registry_missing_run_id_does_not_crash(tmp_path: Path) -> None:
    """Resuming a run with no registry entry (started elsewhere) is fine —
    mark_finished is a documented no-op for unknown run_ids."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    # An empty registry: this run was never registered here.
    registry = Registry(tmp_path / "registry.json")

    result = run_resume(
        "epic-99",
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        root=tmp_path,
        registry=registry,
    )

    assert result.completed == 2  # resume still succeeds
    assert registry.records() == []  # no entry conjured for an unknown run


def test_resume_registry_io_error_is_swallowed(tmp_path: Path) -> None:
    """A registry IO failure must never fail an otherwise-good resume —
    best-effort, exactly like the build path's _registry_finish (#121)."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    class _BoomRegistry(Registry):
        def mark_finished(self, *args, **kwargs):  # type: ignore[override]
            raise OSError("registry unwritable")

    registry = _BoomRegistry(tmp_path / "registry.json")

    result = run_resume(
        "epic-99",
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        root=tmp_path,
        registry=registry,
    )

    assert result.completed == 2  # resume returns its normal result despite IO error


# --- 19.1-001: composite (multi-scope) resume ------------------------------

_EPIC_34_MINI = """# Epic 34

##### Story 34.1-001: Alpha
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _make_composite_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    (stories / "epic-34-mini.md").write_text(_EPIC_34_MINI, encoding="utf-8")
    return tmp_path


def _seed_composite_interrupted(db_path: Path) -> str:
    """A composite run over epic-34 + epic-99, scope stored canonically.

    34.1-001 + 99.1-001 fully DONE; 99.1-002 interrupted mid-review. The run is
    left IN_PROGRESS with the canonical (sorted) scope a real build would persist.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-34,epic-99", "serial")
    ledger.set_total(run_id, 3)
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({"skip_coverage": True, "coverage_threshold": 90}),
    )
    ledger.story_upsert(run_id, "34.1-001", "34", "Alpha", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO")
    for sid in ("34.1-001", "99.1-001"):
        for stage in ("build", "review", "merge"):
            ledger.stage_start(run_id, sid, stage, 1)
            ledger.stage_finish(run_id, sid, stage, 1, "DONE")
        ledger.set_story_status(run_id, sid, "DONE")
    ledger.stage_start(run_id, "99.1-002", "build", 1)
    ledger.stage_finish(run_id, "99.1-002", "build", 1, "DONE")
    ledger.stage_start(run_id, "99.1-002", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")
    return run_id


def test_latest_resumable_run_matches_composite_scope_any_order(tmp_path: Path) -> None:
    """AC5: a composite run is found regardless of scope token order/form."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_composite_interrupted(db)
    assert Ledger(db).latest_resumable_run("epic-34,epic-99") == run_id
    assert Ledger(db).latest_resumable_run("epic-99,epic-34") == run_id  # reversed
    assert Ledger(db).latest_resumable_run("epic-99 epic-34") == run_id  # spaced
    # A single sub-scope is not the composite run.
    assert Ledger(db).latest_resumable_run("epic-99") is None


def test_resume_composite_scope_order_independent(tmp_path: Path) -> None:
    """AC5: `resume` over the same epics in any order resumes the same run."""
    _make_composite_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_composite_interrupted(db)

    result = run_resume(
        "epic-99 epic-34",  # opposite order + space form of the stored scope
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        root=tmp_path,
    )

    assert result.run_id == run_id          # same run, order-independent
    assert result.nothing_to_resume is False
    assert result.completed == 3            # all three stories end DONE
    assert result.resumed == 1              # only 99.1-002 was re-run


def test_resume_cli_accepts_multiple_positionals(tmp_path: Path, monkeypatch) -> None:
    """AC5: the `resume` command accepts several positionals and canonicalises
    them into one label before resolving the run."""
    import sdlc.resume as resume_mod
    from typer.testing import CliRunner

    from sdlc.cli import app

    captured: dict[str, str] = {}

    def _fake_run_resume(scope, **kwargs):
        captured["scope"] = scope
        return ResumeResult(run_id=None, nothing_to_resume=True)

    monkeypatch.setattr(resume_mod, "run_resume", _fake_run_resume)
    result = CliRunner().invoke(app, ["resume", "epic-18", "epic-15"])
    assert result.exit_code == 0, result.output
    assert captured["scope"] == "epic-15,epic-18"


# --- Issue #537: git-landed done-skips must not block on resume -------------
#
# `_filter_git_landed` (#227) also feeds run_build's done_skips: work already
# merged on the base branch whose markdown was never flipped to Status: Done.
# Those stories carry `.done == False`, so #536's markdown signal misses them
# and they cascade-block their dependents exactly as #536 described.

_GIT_LANDED_EPIC = """# Epic 97

##### Story 97.1-001: Landed but not marked
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 97.2-001: Unfinished
**Priority**: P1
**Points**: 2
**Dependencies**: Story 97.1-001.
"""


def _make_git_landed_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    (stories / "epic-97-landed.md").write_text(_GIT_LANDED_EPIC, encoding="utf-8")
    return tmp_path


def _seed_git_landed_parked(db_path: Path, *, mode: str = "serial") -> str:
    """97.1-001 is SKIPPED but its markdown is NOT ``Status: Done`` — the shape
    `_filter_git_landed` produces. 97.2-001 depends on it and parked mid-review."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-97", mode)
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({"skip_coverage": True, "concurrency": 3}),
    )
    ledger.story_upsert(
        run_id, "97.1-001", "97", "Landed but not marked", "P1", 1,
        "general-purpose", "", None, "SKIPPED",
    )
    ledger.story_upsert(
        run_id, "97.2-001", "97", "Unfinished", "P1", 2,
        "general-purpose", "", None, "TODO",
    )
    ledger.stage_start(run_id, "97.2-001", "build", 1)
    ledger.stage_finish(run_id, "97.2-001", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "97.2-001", 1)
    ledger.stage_start(run_id, "97.2-001", "review", 1)
    ledger.set_story_status(run_id, "97.2-001", "NEEDS_ATTENTION")
    return run_id


def test_resume_dispatches_story_whose_dep_was_git_landed(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #537: a dependency skipped because git shows it landed must not
    block, even though its markdown never said ``Status: Done``."""
    _make_git_landed_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_git_landed_parked(db)

    def fake_landed(buildable, done_skips, root=None):
        landed = [s for s in buildable if s.id == "97.1-001"]
        rest = [s for s in buildable if s.id != "97.1-001"]
        return rest, done_skips + landed

    monkeypatch.setattr(resume_mod, "_filter_git_landed", fake_landed)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-97", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert ("review", "97.2-001") in dispatcher.calls
    assert result.blocked == 0
    assert result.skipped == 1  # the landed dep still tallied via extra_skipped
    assert "97.1-001" not in result.story_status


def test_resume_git_probe_failure_leaves_dep_blocking(
    tmp_path: Path, monkeypatch
) -> None:
    """Offline-safe: when the probe reports nothing landed, the SKIPPED dep keeps
    its pre-#537 blocking behaviour rather than being silently treated as done."""
    _make_git_landed_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_git_landed_parked(db)

    monkeypatch.setattr(
        resume_mod, "_filter_git_landed",
        lambda buildable, done_skips, root=None: (buildable, done_skips),
    )
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-97", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )

    assert ("review", "97.2-001") not in dispatcher.calls
    assert result.blocked == 1


# --- frozen harness routing (Issue #543) ------------------------------------
#
# The run's effective role->harness map (`--harness` > repo `.sdlc-harness.yaml` >
# registry `default:`) is frozen on the run row at creation, exactly as Story
# 28.4-001 freezes model routing. Before the freeze, resume reconstructed
# BuildOptions with the dataclass's empty `harness_map`, so a Codex-routed run
# silently finished on the built-in Claude harness and the ledger recorded Claude.

_HARNESS_EPIC = """# Epic 96

##### Story 96.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


class _HarnessRecordingDispatcher(FakeDispatcher):
    """FakeDispatcher that also records the routed ``agent_cmd`` per stage."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_cmds: dict[str, list[str] | None] = {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.agent_cmds[agent_type] = kwargs.get("agent_cmd")
        return super().__call__(agent_type, prompt, story=story, **kwargs)


def _make_harness_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-96-sample.md").write_text(_HARNESS_EPIC, encoding="utf-8")
    return tmp_path


def _seed_harness_interrupted(db_path: Path, harness_map: dict | None) -> str:
    """One story with build DONE and review interrupted — resume owes review."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-96", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config", json.dumps({"skip_coverage": True})
    )
    ledger.story_upsert(
        run_id, "96.1-001", "96", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.stage_start(run_id, "96.1-001", "build", 1)
    ledger.stage_finish(run_id, "96.1-001", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "96.1-001", 100)
    ledger.stage_start(run_id, "96.1-001", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "96.1-001", "IN_PROGRESS")
    if harness_map is not None:
        ledger.run_set_harness_routing(run_id, harness_map)
    return run_id


def _stage_harness_rows(ledger: Ledger, run_id: str) -> dict[str, str]:
    """The harness recorded for each stage's *latest* attempt."""
    return {
        row["stage_name"]: row["harness"] for row in ledger.state_rows(run_id)
    }


def test_resume_dispatches_on_the_runs_frozen_repo_default_harness(
    tmp_path: Path,
) -> None:
    """Issue #543: a repo defaulting every role to codex must resume on codex."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    every_role_codex = {
        role: "codex"
        for role in ("build", "coverage", "review", "merge", "docs")
    }
    run_id = _seed_harness_interrupted(db, every_role_codex)

    dispatcher = _HarnessRecordingDispatcher()
    run_resume("epic-96", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    # The owed stages actually ran the codex adapter's argv, not `claude -p`.
    assert dispatcher.agent_cmds["review"] is not None
    assert "codex-build-adapter.sh" in dispatcher.agent_cmds["review"][0]
    # ...and the ledger attributes the resumed attempts to codex, not claude.
    assert _stage_harness_rows(Ledger(db), run_id)["review"] == "codex"


def test_resume_honours_a_frozen_per_role_harness_map(tmp_path: Path) -> None:
    """A per-role map replays per role: codex review, built-in claude merge."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_harness_interrupted(db, {"review": "codex"})

    dispatcher = _HarnessRecordingDispatcher()
    run_resume("epic-96", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    rows = _stage_harness_rows(Ledger(db), run_id)
    assert rows["review"] == "codex"
    assert rows["merge"] == "claude"  # unmapped role keeps the built-in default
    assert "codex-build-adapter.sh" in dispatcher.agent_cmds["review"][0]
    assert "claude" in dispatcher.agent_cmds["merge"][0]


def test_resume_replays_the_frozen_map_over_a_changed_repo_file(
    tmp_path: Path, monkeypatch
) -> None:
    """An edit to `.sdlc-harness.yaml` cannot move an in-progress run's routing."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_harness_interrupted(db, {"review": "codex"})

    monkeypatch.chdir(tmp_path)
    Path(".sdlc-harness.yaml").write_text(
        "harness:\n  default: qwen\n", encoding="utf-8"
    )
    dispatcher = _HarnessRecordingDispatcher()
    run_resume("epic-96", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    assert _stage_harness_rows(Ledger(db), run_id)["review"] == "codex"


def test_resume_of_a_run_with_no_frozen_map_stays_on_the_default_harness(
    tmp_path: Path,
) -> None:
    """Back-compat: a run created before the freeze resumes exactly as it ran."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_harness_interrupted(db, None)

    dispatcher = _HarnessRecordingDispatcher()
    run_resume("epic-96", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    # No map → no agent_cmd override at all (byte-identical to the pre-#543 path).
    assert dispatcher.agent_cmds["review"] is None
    assert _stage_harness_rows(Ledger(db), run_id)["review"] == "claude"


def test_run_harness_routing_is_empty_when_the_ledger_file_is_absent(
    tmp_path: Path,
) -> None:
    assert Ledger(tmp_path / "absent.db").run_harness_routing("nope") == {}


def test_run_harness_routing_degrades_to_empty_on_a_corrupt_snapshot(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-96", "serial")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE runs SET harness_routing = 'not json' WHERE id = ?", (run_id,)
        )
    assert ledger.run_harness_routing(run_id) == {}


def test_run_harness_routing_degrades_to_empty_on_a_non_mapping_snapshot(
    tmp_path: Path,
) -> None:
    """Valid JSON of the wrong shape degrades like corrupt JSON, not by raising.

    `'not json'` is caught by the decode guard; a JSON list decodes cleanly and
    would reach the dict comprehension, so it needs its own guard — otherwise a
    hand-edited snapshot turns every resume of that run into a crash.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-96", "serial")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE runs SET harness_routing = ? WHERE id = ?",
            (json.dumps(["build=codex"]), run_id),
        )
    assert ledger.run_harness_routing(run_id) == {}


def test_run_harness_routing_drops_non_string_entries(tmp_path: Path) -> None:
    """A hand-edited snapshot cannot inject a non-name into dispatch routing."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-96", "serial")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE runs SET harness_routing = ? WHERE id = ?",
            (json.dumps({"review": "codex", "merge": None, "build": 7}), run_id),
        )
    assert ledger.run_harness_routing(run_id) == {"review": "codex"}


def test_run_harness_routing_is_empty_on_a_pre_migration_ledger(
    tmp_path: Path,
) -> None:
    """A `runs` table without the column reads as "no routing", never crashes."""
    db = tmp_path / ".sdlc-state.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE runs ("
            "id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
            "finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
            "completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
            "status TEXT NOT NULL, actor TEXT)"
        )
        conn.execute(
            "INSERT INTO runs(id, scope, mode, status, started_at) "
            "VALUES ('legacy', 'epic-96', 'serial', 'IN_PROGRESS', CURRENT_TIMESTAMP)"
        )
    assert Ledger(db).run_harness_routing("legacy") == {}

    # Migration 17 adds the column; the row stays unrouted (no backfill needed —
    # that run genuinely dispatched on the built-in default).
    Ledger(db).ensure_migrated()
    assert Ledger(db).run_harness_routing("legacy") == {}


def test_run_build_freezes_the_effective_harness_map_on_the_run_row(
    tmp_path: Path,
) -> None:
    """The other half of the round-trip: build freezes what resume replays.

    `sdlc build` resolves `--harness` > repo `.sdlc-harness.yaml` > registry
    `default:` into ``opts.harness_map`` before dispatch; run creation stamps that
    resolved map on the run row so the resume has something to restore.
    """
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    opts = BuildOptions(
        scope="epic-96", skip_coverage=True, skip_preflight=True, sequential=True,
        harness_map={"review": "codex"},
    )
    run_build(
        opts,
        queue=discover_queue("epic-96", tmp_path),
        ledger=ledger,
        dispatcher=_HarnessRecordingDispatcher(),
        preflight=lambda: True,
        root=tmp_path,
    )
    assert ledger.run_harness_routing(ledger.latest_run_id()) == {"review": "codex"}


def test_run_build_freezes_an_empty_map_for_an_unrouted_run(tmp_path: Path) -> None:
    """No routing stays no routing — the frozen `{}` is not mistaken for a map."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_build(
        BuildOptions(
            scope="epic-96", skip_coverage=True, skip_preflight=True, sequential=True
        ),
        queue=discover_queue("epic-96", tmp_path),
        ledger=ledger,
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        root=tmp_path,
    )
    assert ledger.run_harness_routing(ledger.latest_run_id()) == {}


# --- Migration 17 backfill (Issue #543) --------------------------------------
#
# A run created *before* the freeze resolved a map and dispatched on it but
# persisted it nowhere. Leaving the new column NULL would read as "unrouted" and
# resume a Codex repo's in-flight run onto Claude — the reported defect itself.
# The backfill recovers the map from the run's own `harness routing:` event
# (written at run creation, before any dispatch), falling back to unanimous
# recorded stage harnesses for runs predating that event line.


def _seed_pre_migration_run(
    db_path: Path,
    *,
    routing_event: str | None = None,
    stage_harnesses: tuple[tuple[str, str], ...] = (),
) -> str:
    """A `runs` row on a ledger whose schema predates Migration 17."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE runs ("
            "id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
            "finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
            "completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
            "status TEXT NOT NULL, actor TEXT, model_routing TEXT)"
        )
        conn.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, story_id TEXT, "
            "ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, "
            "source TEXT, message TEXT NOT NULL, stage TEXT, kind TEXT)"
        )
        conn.execute(
            "CREATE TABLE stages ("
            "run_id TEXT NOT NULL, story_id TEXT NOT NULL, stage_name TEXT NOT NULL, "
            "attempt INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, "
            "started_at TIMESTAMP, harness TEXT)"
        )
        conn.execute(
            "INSERT INTO runs(id, scope, mode, status, started_at) "
            "VALUES ('old', 'epic-96', 'serial', 'IN_PROGRESS', CURRENT_TIMESTAMP)"
        )
        if routing_event is not None:
            conn.execute(
                "INSERT INTO events(run_id, story_id, level, source, message) "
                "VALUES ('old', '', 'info', 'harness', ?)",
                (routing_event,),
            )
        for i, (stage_name, harness) in enumerate(stage_harnesses, start=1):
            conn.execute(
                "INSERT INTO stages(run_id, story_id, stage_name, attempt, status, "
                "harness) VALUES ('old', '96.1-001', ?, ?, 'DONE', ?)",
                (stage_name, i, harness),
            )
    return "old"


def test_backfill_recovers_the_map_from_the_runs_routing_event(
    tmp_path: Path,
) -> None:
    """The reported shape: a repo defaulting to codex, interrupted pre-upgrade."""
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db,
        routing_event=(
            "harness routing: build=codex coverage=codex review=codex "
            "merge=codex docs=codex"
        ),
    )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old") == {
        "build": "codex", "coverage": "codex", "review": "codex",
        "merge": "codex", "docs": "codex",
    }


def test_backfill_recovers_a_mixed_per_role_map_from_the_event(tmp_path: Path) -> None:
    """A partly-Codex run is restored per role, not flattened onto one harness."""
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db,
        routing_event=(
            "harness routing: build=claude coverage=claude review=codex "
            "merge=claude docs=claude"
        ),
    )
    Ledger(db).ensure_migrated()

    recovered = Ledger(db).run_harness_routing("old")
    assert recovered["review"] == "codex"
    assert recovered["build"] == "claude"


def test_backfill_prefers_the_event_over_damaged_stage_rows(tmp_path: Path) -> None:
    """A pre-fix resume that mis-dispatched on Claude cannot corrupt the recovery.

    The routing event is written at run creation, before any dispatch, so it still
    states codex even though the damaged resume recorded a claude stage row.
    """
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db,
        routing_event=(
            "harness routing: build=codex coverage=codex review=codex "
            "merge=codex docs=codex"
        ),
        stage_harnesses=(("build", "codex"), ("review", "claude")),
    )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old")["review"] == "codex"


def test_backfill_never_reroutes_a_run_from_stage_rows_alone(tmp_path: Path) -> None:
    """Recovery is exact-or-nothing: recorded stage harnesses are not evidence.

    Every recorded stage here ran on codex, which *looks* like a whole-repo default
    — but it is indistinguishable from a mixed ``build=codex,review=claude`` map
    whose Claude roles simply had not run yet. Inferring "all codex" would silently
    move `review` onto a different agent and permissions posture, so a run with no
    routing event keeps today's behaviour instead.
    """
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db, stage_harnesses=(("build", "codex"), ("coverage", "codex")),
    )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old") == {}


def test_backfill_leaves_an_ambiguous_legacy_run_alone(tmp_path: Path) -> None:
    """Mixed stage rows with no event are likewise never guessed at."""
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db, stage_harnesses=(("build", "codex"), ("review", "claude")),
    )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old") == {}


def test_backfill_leaves_a_genuinely_unrouted_run_unrouted(tmp_path: Path) -> None:
    """No event and only claude stages → the empty-map fast path is preserved."""
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db, stage_harnesses=(("build", "claude"), ("review", "claude")),
    )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old") == {}


def test_backfill_ignores_a_malformed_routing_event(tmp_path: Path) -> None:
    """A partially-parsed map would freeze a run onto a subset of its routing."""
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(db, routing_event="harness routing: build=codex review")
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old") == {}


def test_backfill_never_overwrites_an_already_frozen_map(tmp_path: Path) -> None:
    """Idempotent: re-running migrations leaves a stamped map untouched."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-96", "serial")
    ledger.run_set_harness_routing(run_id, {"review": "codex"})
    ledger.event_log(
        run_id, "", "info", "harness",
        "harness routing: build=qwen coverage=qwen review=qwen merge=qwen docs=qwen",
    )
    ledger.ensure_migrated()

    assert ledger.run_harness_routing(run_id) == {"review": "codex"}


def test_backfill_takes_the_earliest_routing_event_for_a_run(tmp_path: Path) -> None:
    """The run-creation line wins over any later duplicate.

    Only the first line is written before the first dispatch, so only the first
    is guaranteed uncorrupted by a resume this bug already mis-routed. A later
    line naming a different map must not overwrite it.
    """
    db = tmp_path / ".sdlc-state.db"
    _seed_pre_migration_run(
        db,
        routing_event=(
            "harness routing: build=codex coverage=codex review=codex "
            "merge=codex docs=codex"
        ),
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events(run_id, story_id, level, source, message) "
            "VALUES ('old', '', 'info', 'harness', ?)",
            (
                "harness routing: build=claude coverage=claude review=claude "
                "merge=claude docs=claude",
            ),
        )
    Ledger(db).ensure_migrated()

    assert Ledger(db).run_harness_routing("old")["review"] == "codex"


def test_parse_harness_routing_event_ignores_a_non_routing_line() -> None:
    """The prefix guard, exercised directly.

    The backfill's SQL pre-filters on the same prefix, so this branch is not
    reachable through the migration — but the parser is a module-level helper and
    a future caller without that filter must get `{}`, not a map parsed out of an
    unrelated `harness` event such as the preflight capability lines.
    """
    assert _parse_harness_routing_event("harness 'claude': ok (default slot)") == {}
    assert _parse_harness_routing_event("") == {}


def test_resume_of_a_backfilled_pre_upgrade_run_dispatches_on_codex(
    tmp_path: Path,
) -> None:
    """End-to-end: the upgrade path the issue reports, from ledger to dispatch."""
    _make_harness_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    # A pre-Migration-17 ledger carrying a routed, interrupted run.
    run_id = _seed_harness_interrupted(db, None)
    ledger = Ledger(db)
    ledger.event_log(
        run_id, "", "info", "harness",
        "harness routing: build=codex coverage=codex review=codex "
        "merge=codex docs=codex",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runs SET harness_routing = NULL WHERE id = ?", (run_id,))
        conn.execute("DELETE FROM _migrations WHERE version = 17")
    ledger.ensure_migrated()

    dispatcher = _HarnessRecordingDispatcher()
    run_resume("epic-96", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    assert "codex-build-adapter.sh" in dispatcher.agent_cmds["review"][0]
    assert _stage_harness_rows(Ledger(db), run_id)["review"] == "codex"


# --- fix-issue runs (#547) ---------------------------------------------------
#
# `run_resume` derived the correct resume point from the ledger, then intersected
# it with a queue rebuilt from the markdown epics — where an `issue-<N>` scope has
# no story. The queue came out empty, nothing dispatched, and the close-out marked
# the run DONE: silent, exit 0, and destructive, since the run became terminal and
# could never be resumed even after the bug was fixed.


def _fix_payload(agent_type: str) -> dict:
    return {
        "investigation": {
            "root_cause": "off-by-one", "complexity": "LOW",
            "fix_approach": "clamp", "files_to_modify": ["a.py"],
            "risk": "low", "investigation_status": "READY",
        },
        "build": {
            "branch_name": "feature/issue-7", "build_status": "SUCCESS",
            "commit_sha": "deadbeef",
        },
        "coverage": {
            "pr_number": 100, "pr_url": "https://example/pull/100",
            "coverage_pct": 95.0, "tests_added": 2, "coverage_status": "PASS",
        },
        "review": {
            "pr_number": 100, "approval_status": "APPROVED",
            "change_count": 0, "final_status": "APPROVED",
        },
        "merge": {
            "pr_number": 100, "merge_status": "MERGED",
            "merge_sha": "cafef00d", "merged_at": "2026-07-15T00:00:00Z",
        },
        "summary": {"summary_markdown": "## done"},
    }[agent_type]


class _FixDispatcher:
    """A fix-pipeline dispatcher (its call shape differs from the build one)."""

    def __init__(self) -> None:
        self.agents: list[str] = []

    def __call__(self, agent_type, prompt, *, story=None, model=None,
                 transcript_path=None, on_progress=None, **kwargs):
        from sdlc.dispatch import AgentResult

        self.agents.append(agent_type)
        if transcript_path is not None:
            Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
            Path(transcript_path).write_text("{}", encoding="utf-8")
        return AgentResult(
            agent_type=agent_type, data=_fix_payload(agent_type), raw=""
        )


def _seed_fix_run_interrupted(tmp_path: Path) -> str:
    """A real `sdlc fix` run killed at the coverage boundary (build DONE)."""
    from sdlc.fix_issue import FixOptions, run_fix
    from sdlc.issue_host import RunResult

    class _Stop(Exception):
        pass

    def gh(argv, timeout=None):
        joined = " ".join(argv)
        if "issue view" in joined:
            return RunResult(0, json.dumps({
                "number": 7, "title": "Bug", "body": "boom", "state": "OPEN",
                "assignees": [], "labels": [{"name": "bug"}],
            }), "")
        return RunResult(0, "me", "")

    inner = _FixDispatcher()

    def killer(agent_type, prompt, **kwargs):
        if agent_type == "coverage":
            raise _Stop()
        return inner(agent_type, prompt, **kwargs)

    db = tmp_path / ".sdlc-state.db"
    try:
        run_fix(
            FixOptions(issue=7), ledger=Ledger(db), dispatcher=killer,
            preflight=lambda: True, runner=gh, root=tmp_path,
        )
    except _Stop:
        pass
    return Ledger(db).latest_run_id()


def _fix_gh(argv, timeout=None):
    from sdlc.issue_host import RunResult

    if "issue view" in " ".join(argv):
        return RunResult(0, json.dumps({
            "number": 7, "title": "Bug", "body": "boom", "state": "OPEN",
            "assignees": [], "labels": [{"name": "bug"}],
        }), "")
    return RunResult(0, "me", "")


def test_resume_drives_a_fix_run_instead_of_closing_it_out(tmp_path: Path) -> None:
    """The reported defect: `sdlc resume` must continue the fix, not bury it."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_fix_run_interrupted(tmp_path)
    dispatcher = _FixDispatcher()

    result = run_resume(
        "issue-7", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        run_id=run_id, runner=_fix_gh,
    )

    assert result.nothing_to_resume is False
    assert {"coverage", "review", "merge"}.issubset(set(dispatcher.agents))
    assert "build" not in dispatcher.agents  # already DONE, never redone
    assert (Ledger(db).run_row(run_id) or {})["status"] == "DONE"


def test_resume_never_closes_out_an_undispatchable_fix_run(tmp_path: Path) -> None:
    """A fix run resume cannot drive must stay resumable, never be marked DONE.

    This is the destructive half of #547: the old path closed the run terminal on
    an empty queue, so the work could not be recovered later even by a fixed
    resume. Simulated by removing the recorded plan, which `resume_fix` refuses on.
    """
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_fix_run_interrupted(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "DELETE FROM events WHERE run_id = ? AND source = 'fix-plan'", (run_id,)
        )

    dispatcher = _FixDispatcher()
    run_resume(
        "issue-7", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        run_id=run_id, runner=_fix_gh,
    )

    assert dispatcher.agents == []
    assert (Ledger(db).run_row(run_id) or {})["status"] == "IN_PROGRESS"


def test_resume_preserves_a_fix_runs_mode(tmp_path: Path) -> None:
    """Resume re-stamps `runs.mode` for builds; a fix run must keep its lineage.

    Overwriting `fix` with `serial`/`parallel` erases the marker that tells the
    dashboard — and any later resume — which pipeline the run belongs to.
    """
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_fix_run_interrupted(tmp_path)

    run_resume(
        "issue-7", ledger=Ledger(db), dispatcher=_FixDispatcher(), root=tmp_path,
        run_id=run_id, runner=_fix_gh,
    )

    assert (Ledger(db).run_row(run_id) or {})["mode"] == "fix"


def test_run_resume_refuses_a_live_owner_for_a_fix_run(tmp_path: Path) -> None:
    """The live-owner guard (#595) also covers a resumed `sdlc fix` run.

    `run_resume` hands a fix-mode run straight to `resume_fix`; the refusal must
    surface its exact message (not the generic "nothing to resume" every other
    `resume_fix` abort collapses into) so the operator sees the owning pid.
    """
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_fix_run_interrupted(tmp_path)
    registry = _seed_registry_live(tmp_path, db, run_id, "issue-7")
    dispatcher = _FixDispatcher()

    result = run_resume(
        "issue-7", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        run_id=run_id, runner=_fix_gh, registry=registry,
    )

    assert result.refused is True
    assert result.nothing_to_resume is False
    assert run_id in result.refusal_reason
    assert str(_OTHER_LIVE_PID) in result.refusal_reason
    assert dispatcher.agents == []
    assert (Ledger(db).run_row(run_id) or {})["status"] == "IN_PROGRESS"


def test_run_resume_fix_run_force_overrides_a_live_owner(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_fix_run_interrupted(tmp_path)
    registry = _seed_registry_live(tmp_path, db, run_id, "issue-7")

    result = run_resume(
        "issue-7", ledger=Ledger(db), dispatcher=_FixDispatcher(), root=tmp_path,
        run_id=run_id, runner=_fix_gh, registry=registry, force=True,
    )

    assert result.refused is False
    assert (Ledger(db).run_row(run_id) or {})["status"] == "DONE"


def test_resume_fix_run_surfaces_a_live_owner_refusal_from_resume_fix(
    tmp_path: Path, monkeypatch
) -> None:
    """`_resume_fix_run` must translate `resume_fix`'s own refusal, not just
    `run_resume`'s pre-check.

    `run_resume` and `resume_fix` (fix_issue.py) each run the #595 guard
    independently — `run_resume` checks before dispatch, `resume_fix` checks
    again right before mutating the ledger, so a live owner registered in the
    gap between the two is still caught. That inner refusal must collapse into
    the same `refused`/`refusal_reason` result as the outer one, not the
    generic `nothing_to_resume` every other `resume_fix` abort produces.
    """
    import sdlc.fix_issue as fix_issue_mod
    from sdlc.fix_issue import FixResult

    run_id = _seed_fix_run_interrupted(tmp_path)

    def fake_resume_fix(*args, **kwargs):
        return FixResult(
            issue=7, run_id=run_id, aborted=True, status="ABORTED",
            abort_reason="inner refusal", live_owner_blocked=True,
        )

    monkeypatch.setattr(fix_issue_mod, "resume_fix", fake_resume_fix)

    result = resume_mod._resume_fix_run(run_id, ledger=Ledger(tmp_path / ".sdlc-state.db"))

    assert result.refused is True
    assert result.nothing_to_resume is False
    assert result.refusal_reason == "inner refusal"
