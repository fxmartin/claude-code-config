# ABOUTME: Tests for controller-native rollback (Story 10.2-001).
# ABOUTME: Seeds a multi-story ledger, rolls back to a checkpoint, asserts guards.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.build import Ledger
from sdlc.rollback import (
    RollbackError,
    RollbackResult,
    list_checkpoints,
    run_rollback,
)

# A three-story epic-99 project, coverage gate off so the pipeline is
# build -> review -> merge.
_THREE_STORY_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 99.1-002: Two
**Priority**: P2
**Points**: 2
**Dependencies**: None.

##### Story 99.1-003: Three
**Priority**: P2
**Points**: 2
**Dependencies**: None.
"""


def _make_three_story_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_THREE_STORY_EPIC, encoding="utf-8")
    return tmp_path


def _seed_three(db_path: Path, *, third_merged: bool = False) -> str:
    """A three-story run.

    99.1-001: build/review/merge DONE, PR #100, status DONE   (merged → keep).
    99.1-002: build+review DONE, NOT merged, PR #101, IN_PROGRESS (resettable).
    99.1-003: build DONE only, PR #102, IN_PROGRESS (resettable). When
              ``third_merged`` it also has a merge DONE stage + status DONE so
              the merged-PR guard must refuse a rollback that would reset it.
    Run left IN_PROGRESS. skip_coverage.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 3)
    ledger.event_log(run_id, "", "info", "config", json.dumps({"skip_coverage": True}))

    ledger.story_upsert(run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-003", "99", "Three", "P2", 2, "general-purpose", "", None, "TODO")

    # 99.1-001 fully merged.
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "DONE")

    # 99.1-002 built + reviewed, never merged.
    for stage in ("build", "review"):
        ledger.stage_start(run_id, "99.1-002", stage, 1)
        ledger.stage_finish(run_id, "99.1-002", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-002", 101)
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")

    # 99.1-003 built only.
    ledger.stage_start(run_id, "99.1-003", "build", 1)
    ledger.stage_finish(run_id, "99.1-003", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-003", 102)
    if third_merged:
        for stage in ("review", "merge"):
            ledger.stage_start(run_id, "99.1-003", stage, 1)
            ledger.stage_finish(run_id, "99.1-003", stage, 1, "DONE")
        ledger.set_story_status(run_id, "99.1-003", "DONE")
    else:
        ledger.set_story_status(run_id, "99.1-003", "IN_PROGRESS")
    return run_id


# --- checkpoint listing ----------------------------------------------------


def test_list_checkpoints_in_execution_order(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db)
    cps = list_checkpoints(Ledger(db), rid)
    assert [c["story_id"] for c in cps] == ["99.1-001", "99.1-002", "99.1-003"]
    # The merged flag distinguishes committed checkpoints.
    by_id = {c["story_id"]: c for c in cps}
    assert by_id["99.1-001"]["merged"] is True
    assert by_id["99.1-002"]["merged"] is False


# --- run_rollback ----------------------------------------------------------


def test_rollback_resets_stories_after_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db)

    result = run_rollback(Ledger(db), rid, "99.1-001")

    assert isinstance(result, RollbackResult)
    assert result.checkpoint == "99.1-001"
    assert result.kept_stories == ["99.1-001"]
    assert result.reset_stories == ["99.1-002", "99.1-003"]

    ledger = Ledger(db)
    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    # Checkpoint untouched.
    assert rows["99.1-001"]["status"] == "DONE"
    assert rows["99.1-001"]["pr_number"] == 100
    # Reset stories returned to TODO, PR cleared, no stage rows remain.
    assert rows["99.1-002"]["status"] == "TODO"
    assert rows["99.1-002"]["pr_number"] is None
    assert rows["99.1-003"]["status"] == "TODO"
    breakdown = ledger.stage_breakdown(rid)
    assert "99.1-002" not in breakdown or breakdown["99.1-002"] == []
    assert "99.1-003" not in breakdown or breakdown["99.1-003"] == []
    # The checkpoint keeps its three stage rows.
    assert len(breakdown["99.1-001"]) == 3
    # Run reopened so resume/build picks it up again.
    assert ledger.run_row(rid)["status"] == "IN_PROGRESS"


def test_rollback_to_latest_run_when_run_id_omitted(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db)
    result = run_rollback(Ledger(db), None, "99.1-002")
    assert result.reset_stories == ["99.1-003"]
    assert result.kept_stories == ["99.1-001", "99.1-002"]


def test_rollback_checkpoint_is_last_story_is_noop(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db)
    result = run_rollback(Ledger(db), rid, "99.1-003")
    assert result.reset_stories == []
    # Nothing changed: 99.1-003 keeps its build stage row.
    assert len(Ledger(db).stage_breakdown(rid)["99.1-003"]) == 1


# --- guard rails -----------------------------------------------------------


def test_rollback_refuses_unknown_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db)
    with pytest.raises(RollbackError) as exc:
        run_rollback(Ledger(db), rid, "99.9-999")
    assert "99.9-999" in str(exc.value)
    # Ledger untouched.
    assert Ledger(db).story_rows(rid)[1]["status"] == "IN_PROGRESS"


def test_rollback_refuses_when_reset_would_discard_merged_pr(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db, third_merged=True)
    with pytest.raises(RollbackError) as exc:
        run_rollback(Ledger(db), rid, "99.1-001")
    msg = str(exc.value)
    assert "merged" in msg.lower()
    assert "99.1-003" in msg
    # Refusal leaves every story exactly as it was.
    ledger = Ledger(db)
    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    assert rows["99.1-002"]["status"] == "IN_PROGRESS"
    assert rows["99.1-003"]["status"] == "DONE"
    assert len(ledger.stage_breakdown(rid)["99.1-003"]) == 3


def test_rollback_no_run_raises(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    with pytest.raises(RollbackError):
        run_rollback(Ledger(db), None, "99.1-001")


# --- resume rebuilds only the rolled-back stories --------------------------


def test_resume_rebuilds_only_rolled_back_stories(tmp_path: Path) -> None:
    """End-to-end: roll a multi-stage run back one checkpoint, then resume and
    assert only the rolled-back stories are rebuilt (the checkpoint stays)."""
    _make_three_story_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    rid = _seed_three(db)

    run_rollback(Ledger(db), rid, "99.1-001")

    from sdlc.resume import run_resume

    from test_build import FakeDispatcher

    dispatcher = FakeDispatcher()
    result = run_resume("epic-99", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)

    # The checkpoint is never rebuilt.
    assert ("build", "99.1-001") not in dispatcher.calls
    # Both rolled-back stories rebuild from the top (build stage runs again).
    assert ("build", "99.1-002") in dispatcher.calls
    assert ("build", "99.1-003") in dispatcher.calls

    ledger = Ledger(db)
    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    assert rows["99.1-001"]["status"] == "DONE"
    assert rows["99.1-002"]["status"] == "DONE"
    assert rows["99.1-003"]["status"] == "DONE"
    assert result.completed == 3
