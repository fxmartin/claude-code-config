# ABOUTME: Tests for controller-native resume (Story 10.1-001).
# ABOUTME: Seeds an interrupted ledger via fixtures, asserts resume re-enters right.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.build import Ledger, run_build
from sdlc.cohort import Story
from sdlc.resume import ResumeResult, compute_resume_plan, run_resume

from test_build import FakeDispatcher  # reuse the canned schema-valid dispatcher

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
