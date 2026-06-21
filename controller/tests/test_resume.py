# ABOUTME: Tests for controller-native resume (Story 10.1-001).
# ABOUTME: Seeds an interrupted ledger via fixtures, asserts resume re-enters right.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.build import Ledger, run_build
from sdlc.cohort import Story
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


def test_resume_all_stages_done_unfinalised_is_noop(tmp_path: Path) -> None:
    """A resumable run whose only story has every stage DONE (just not finalised)
    has nothing incomplete to dispatch — it is a no-op, not a rebuild."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_all_stages_done_unfinalised(db)
    dispatcher = FakeDispatcher()
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)
    assert result.nothing_to_resume is True
    assert result.run_id == rid
    assert dispatcher.calls == []  # nothing was dispatched


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
