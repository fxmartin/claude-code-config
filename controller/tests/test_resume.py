# ABOUTME: Tests for controller-native resume (Story 10.1-001).
# ABOUTME: Seeds an interrupted ledger via fixtures, asserts resume re-enters right.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.build import BuildOptions, Ledger, run_build
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
