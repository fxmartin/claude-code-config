# ABOUTME: Tests for the AWAITING_APPROVAL merge state (Story 12.3-003).
# ABOUTME: A high-risk-blocked merge parks the story without burning the bugfix loop.

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    Ledger,
    _dispatch_stage,
    _merge_awaiting_approval,
    compute_run_terminal,
    run_build,
    status_snapshot,
)
from sdlc.cohort import Story
from sdlc.reconcile import reconcile_run

from test_build import FakeDispatcher, _open, _sample_queue  # reuse canned dispatchers


# ---------------------------------------------------------------------------
# A merge response a high-risk-blocked agent emits: merge_status is FAILED
# (the schema enum has no "blocked" member) plus an additive block_reason.
# ---------------------------------------------------------------------------

def _blocked_merge(_story_id: str = "s1-001") -> dict:
    return {
        "pr_number": 100,
        "merge_status": "FAILED",
        "merge_sha": "0000000",
        "merged_at": "2026-06-12T00:00:00Z",
        "block_reason": "BLOCKED_HIGH_RISK: PR carries risk:high, awaiting human approval",
    }


# ---------------------------------------------------------------------------
# Detection: distinguish a high-risk block from a generic merge failure
# ---------------------------------------------------------------------------

def test_merge_awaiting_approval_detects_block_reason() -> None:
    assert _merge_awaiting_approval(_blocked_merge()) is True


def test_merge_awaiting_approval_detects_risk_label_in_error_summary() -> None:
    data = {
        "pr_number": 1,
        "merge_status": "FAILED",
        "merge_sha": "abc",
        "merged_at": "2026-06-12T00:00:00Z",
        "error_summary": "cannot merge: PR labeled risk:high without risk-approved",
    }
    assert _merge_awaiting_approval(data) is True


def test_merge_awaiting_approval_ignores_a_clean_merge() -> None:
    data = {
        "pr_number": 1,
        "merge_status": "MERGED",
        "merge_sha": "cafef00d",
        "merged_at": "2026-06-12T00:00:00Z",
    }
    assert _merge_awaiting_approval(data) is False


def test_merge_awaiting_approval_ignores_a_generic_failure() -> None:
    data = {
        "pr_number": 1,
        "merge_status": "FAILED",
        "merge_sha": "0",
        "merged_at": "2026-06-12T00:00:00Z",
        "error_summary": "rebase hit a conflict in app.py",
    }
    assert _merge_awaiting_approval(data) is False


# ---------------------------------------------------------------------------
# _dispatch_stage classifies the high-risk block as a distinct kind
# ---------------------------------------------------------------------------

class _BlockedMergeDispatcher(FakeDispatcher):
    """A FakeDispatcher whose merge for ``block_ids`` is high-risk-blocked.

    ``block_ids=None`` blocks every story's merge; otherwise only the listed
    story ids are parked, so dependency-blocking and tally tests can isolate one
    awaiting-approval story from clean ones.
    """

    def __init__(self, overrides=None, block_ids=None) -> None:
        super().__init__(overrides)
        self.block_ids = block_ids

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        sid = getattr(story, "id", "")
        if agent_type == "merge" and (self.block_ids is None or sid in self.block_ids):
            self.calls.append((agent_type, sid))
            return AgentResult(
                agent_type="merge", data=_blocked_merge(sid),
                raw="", usage=None, cost_usd=None, session_id="sess-merge",
            )
        return super().__call__(agent_type, prompt, story=story, **kwargs)


def test_dispatch_stage_returns_awaiting_approval_kind() -> None:
    story = Story("s1-001", "One", "99", "sample", "epic-99.md", "P1", 2, "py", [])
    ok, _result, failure, kind = _dispatch_stage(
        "merge", story, BuildOptions(scope="epic-99"), 100,
        _BlockedMergeDispatcher(),
    )
    assert ok is False
    assert kind == "awaiting_approval"
    assert "risk" in failure.lower() or "approval" in failure.lower()


# ---------------------------------------------------------------------------
# compute_run_terminal precedence
# ---------------------------------------------------------------------------

def test_terminal_awaiting_only_is_awaiting_approval() -> None:
    assert compute_run_terminal(
        {"a": "DONE", "b": "AWAITING_APPROVAL", "c": "SKIPPED"}
    ) == "AWAITING_APPROVAL"


def test_terminal_failed_beats_awaiting() -> None:
    assert compute_run_terminal(
        {"a": "FAILED", "b": "AWAITING_APPROVAL"}
    ) == "FAILED"


def test_terminal_needs_attention_beats_awaiting() -> None:
    assert compute_run_terminal(
        {"a": "NEEDS_ATTENTION", "b": "AWAITING_APPROVAL"}
    ) == "NEEDS_ATTENTION"


def test_terminal_non_terminal_leftover_beats_awaiting() -> None:
    # A crashed-run leftover (IN_PROGRESS) is not DONE/SKIPPED/AWAITING_APPROVAL,
    # so it outranks an awaiting-approval merge: a human must finish it, so the
    # run is NEEDS_ATTENTION rather than honestly parked AWAITING_APPROVAL.
    assert compute_run_terminal(
        {"a": "IN_PROGRESS", "b": "AWAITING_APPROVAL"}
    ) == "NEEDS_ATTENTION"


def test_terminal_all_done_is_done() -> None:
    assert compute_run_terminal({"a": "DONE", "b": "SKIPPED"}) == "DONE"


# ---------------------------------------------------------------------------
# run_build: a high-risk-blocked merge parks AWAITING_APPROVAL, no bugfix
# ---------------------------------------------------------------------------

def test_run_build_high_risk_block_parks_awaiting_approval(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # s1-001's merge is blocked high-risk; s1-002 builds clean; s1-003 depends
    # on s1-001 so it is blocked by the non-DONE dependency.
    dispatcher = _BlockedMergeDispatcher(block_ids={"s1-001"})
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # The blocked story parked AWAITING_APPROVAL — never FAILED.
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.awaiting_approval == 1
    assert result.failed == 0
    # s1-002 is unaffected and merges clean.
    assert result.story_status["s1-002"] == "DONE"
    # The bugfix loop was never entered for the high-risk block.
    assert ("bugfix", "s1-001") not in dispatcher.calls


def test_run_build_high_risk_block_run_terminal_is_awaiting(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # Single story, merge blocked high-risk → run terminal AWAITING_APPROVAL.
    queue = [Story("solo-001", "Solo", "99", "sample", "epic-99.md", "P1", 2, "py", [])]
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=queue,
        ledger=Ledger(db),
        dispatcher=_BlockedMergeDispatcher(),
        preflight=lambda: True,
    )
    conn = _open(db)
    status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert status == "AWAITING_APPROVAL"
    # AWAITING_APPROVAL is terminal: finished_at is stamped.
    finished = conn.execute("SELECT finished_at FROM runs").fetchone()[0]
    assert finished is not None


def test_run_build_dependent_of_awaiting_is_blocked(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=_BlockedMergeDispatcher(block_ids={"s1-001"}),
        preflight=lambda: True,
    )
    # s1-003 depends on the awaiting-approval s1-001 → blocked (dep not DONE).
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.story_status["s1-003"] == "BLOCKED"


# ---------------------------------------------------------------------------
# status_snapshot surfaces the new count
# ---------------------------------------------------------------------------

def test_status_snapshot_counts_awaiting_approval(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[Story("solo-001", "Solo", "99", "sample", "epic-99.md", "P1", 2, "py", [])],
        ledger=Ledger(db),
        dispatcher=_BlockedMergeDispatcher(),
        preflight=lambda: True,
    )
    snap = status_snapshot(Ledger(db))
    assert snap["counts"]["awaiting_approval"] == 1


# ---------------------------------------------------------------------------
# Reconcile-after-approval: the landed branch flips AWAITING_APPROVAL → DONE
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def test_reconcile_flips_approved_awaiting_to_done(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    # The human approved and the PR merged: feature branch landed on main.
    _git(root, "checkout", "-q", "-b", "feature/99.1-001")
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "f.py")
    _git(root, "commit", "-q", "-m", "feat: f (#99.1-001)")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-001")

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "99.1-001", "99", "99.1-001", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "AWAITING_APPROVAL")
    ledger.run_update_status(run_id, "AWAITING_APPROVAL")

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-001"]
    assert result.reclassified[0]["from_status"] == "AWAITING_APPROVAL"
    assert result.run_status_after == "DONE"
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(run_id)}
    assert statuses["99.1-001"] == "DONE"
