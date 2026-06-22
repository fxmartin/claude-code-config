# ABOUTME: Tests for close-out reconciliation against origin/main (Story 12.3-001).
# ABOUTME: Builds real temp git repos, parks landed stories, asserts reclassification.

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.build import Ledger
from sdlc.reconcile import (
    ReconcileResult,
    _compute_terminal,
    _ensure_merge_done,
    _gh_pr_state,
    reconcile_run,
)


# --- git fixture helpers ----------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    """A repo on branch ``main`` with one base commit. No remote."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Isolate from any global hooks (e.g. a gitleaks pre-commit) so fixture
    # commits are deterministic.
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def _commit(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _checkout(root: Path, ref: str, *, new: bool = False) -> None:
    _git(root, "checkout", "-q", *( ["-b"] if new else []), ref)


# --- ledger fixture helpers -------------------------------------------------


def _seed_run(db_path: Path, stories: list[tuple[str, str, int | None]]) -> str:
    """Seed a run with ``(story_id, status, pr_number)`` rows. Run left FAILED."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, len(stories))
    for sid, status, pr in stories:
        ledger.story_upsert(
            run_id, sid, "99", sid, "P1", 1, "general-purpose", "", None, "TODO"
        )
        # Build+review stages happened; merge never recorded (parked).
        for stage in ("build", "review"):
            ledger.stage_start(run_id, sid, stage, 1)
            ledger.stage_finish(run_id, sid, stage, 1, "DONE")
        if pr is not None:
            ledger.set_story_pr(run_id, sid, pr)
        ledger.set_story_status(run_id, sid, status)
    ledger.run_update_status(run_id, "FAILED")
    return run_id


def _status(db_path: Path, run_id: str, story_id: str) -> str:
    return {r["story_id"]: r["status"] for r in Ledger(db_path).story_rows(run_id)}[
        story_id
    ]


def _merge_done(db_path: Path, run_id: str, story_id: str) -> int:
    rows = Ledger(db_path).stage_breakdown(run_id).get(story_id, [])
    return sum(1 for a in rows if a["name"] == "merge" and a["status"] == "DONE")


# --- fast-forward / merge-commit landing (is-ancestor) ----------------------


def test_fast_forward_landing_reclassifies_to_done(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-001", new=True)
    _commit(root, "ff.py", "x = 1\n", "feat: ff (#99.1-001)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-001")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-001", "FAILED", 100)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert isinstance(result, ReconcileResult)
    assert [r["story_id"] for r in result.reclassified] == ["99.1-001"]
    assert result.reclassified[0]["signal"] == "is-ancestor"
    assert result.run_status_before == "FAILED"
    assert result.run_status_after == "DONE"
    assert _status(db, run_id, "99.1-001") == "DONE"
    assert _merge_done(db, run_id, "99.1-001") == 1


# --- patch-id equivalence (git cherry) — transitive/stacked landing ---------


def test_cherry_patch_id_landing_reclassifies(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-002", new=True)
    _commit(root, "stacked.py", "y = 2\n", "feat: stacked (#99.1-002)")
    # Land the same *patch* on main under a different sha (identical diff, new
    # message → equal patch-id, different commit), leaving the feature branch tip
    # NOT an ancestor of main — exactly the transitive/stacked landing shape.
    _checkout(root, "main")
    _commit(root, "stacked.py", "y = 2\n", "feat: landed under a different sha")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-002", "NEEDS_ATTENTION", 101)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-002"]
    assert result.reclassified[0]["signal"] == "git-cherry"
    assert _status(db, run_id, "99.1-002") == "DONE"


# --- squash landing — caught by the (#id) commit tag on main ----------------


def test_squash_landing_caught_by_commit_tag(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    # Multi-commit feature branch: a squash merge breaks per-commit patch-id, so
    # only the (#id) tag on the squashed main commit proves it landed.
    _checkout(root, "feature/99.1-003", new=True)
    _commit(root, "a.py", "a = 1\n", "feat: part a")
    _commit(root, "b.py", "b = 2\n", "feat: part b")
    _checkout(root, "main")
    # Simulate squash: a single new commit carrying the mandated story tag.
    _commit(root, "squashed.py", "a = 1\nb = 2\n", "feat: squashed work (#99.1-003)")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-003", "FAILED", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-003"]
    assert result.reclassified[0]["signal"] == "commit-tag"
    assert _status(db, run_id, "99.1-003") == "DONE"


# --- PR merged but branch deleted (gh signal) -------------------------------


def test_pr_merged_branch_deleted_uses_gh(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)  # no feature/ branch exists at all
    monkeypatch.setattr(
        "sdlc.reconcile._gh_pr_state",
        lambda pr_number, root: "MERGED" if pr_number == 102 else None,
    )

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-004", "NEEDS_ATTENTION", 102)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-004"]
    assert result.reclassified[0]["signal"] == "gh-pr-merged"
    assert _status(db, run_id, "99.1-004") == "DONE"


# --- genuinely unlanded work stays parked -----------------------------------


def test_genuinely_unlanded_stays_parked(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-005", new=True)
    _commit(root, "wip.py", "z = 3\n", "feat: wip (#99.1-005)")
    _checkout(root, "main")  # never merged anywhere
    monkeypatch.setattr("sdlc.reconcile._gh_pr_state", lambda pr_number, root: None)

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-005", "FAILED", 103)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert result.run_status_after == "FAILED"
    assert _status(db, run_id, "99.1-005") == "FAILED"
    assert _merge_done(db, run_id, "99.1-005") == 0


# --- offline / no-remote degrades to a no-op skip ---------------------------


def test_offline_fetch_failure_is_noop_skip(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)  # no origin remote → git fetch origin fails
    _checkout(root, "feature/99.1-006", new=True)
    _commit(root, "done.py", "q = 4\n", "feat: done (#99.1-006)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-006")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-006", "FAILED", 104)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=True)

    assert result.skipped is True
    assert result.reclassified == []
    # Landed work stays parked because we could not refresh remote state.
    assert _status(db, run_id, "99.1-006") == "FAILED"
    assert result.run_status_after == "FAILED"


# --- already DONE / SKIPPED stories are left untouched ----------------------


def test_done_and_skipped_left_untouched(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.story_upsert(run_id, "99.1-007", "99", "Done", "P1", 1, "x", "", None, "TODO")
    ledger.story_upsert(run_id, "99.1-008", "99", "Skip", "P1", 1, "x", "", None, "TODO")
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-007", stage, 1)
        ledger.stage_finish(run_id, "99.1-007", stage, 1, "DONE")
    ledger.set_story_status(run_id, "99.1-007", "DONE")
    ledger.set_story_status(run_id, "99.1-008", "SKIPPED")
    ledger.run_update_status(run_id, "DONE")

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert _merge_done(db, run_id, "99.1-007") == 1  # no duplicate merge row
    assert _status(db, run_id, "99.1-008") == "SKIPPED"


# --- idempotent re-run -------------------------------------------------------


def test_idempotent_rerun(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-009", new=True)
    _commit(root, "i.py", "i = 9\n", "feat: i (#99.1-009)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-009")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-009", "FAILED", 105)])

    first = reconcile_run(Ledger(db), run_id, root=root, fetch=False)
    assert len(first.reclassified) == 1

    second = reconcile_run(Ledger(db), run_id, root=root, fetch=False)
    assert second.reclassified == []
    assert second.run_status_after == "DONE"
    assert _merge_done(db, run_id, "99.1-009") == 1  # still exactly one merge row


# --- no run in ledger -------------------------------------------------------


def test_no_run_is_clean_noop(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    result = reconcile_run(ledger, None, root=tmp_path, fetch=False)
    assert result.reclassified == []
    assert result.run_id == ""


# --- ReconcileResult.changed -------------------------------------------------


def test_changed_property_reflects_flips_and_terminal() -> None:
    # A reclassification alone makes it changed.
    assert ReconcileResult(run_id="r", reclassified=[{"story_id": "x"}]).changed is True
    # A run-terminal transition alone makes it changed.
    assert ReconcileResult(
        run_id="r", run_status_before="FAILED", run_status_after="DONE"
    ).changed is True
    # No flips and a stable terminal is a genuine no-op.
    assert ReconcileResult(
        run_id="r", run_status_before="FAILED", run_status_after="FAILED"
    ).changed is False


# --- _gh_pr_state isolation --------------------------------------------------


def test_gh_pr_state_returns_state_on_success(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="MERGED\n", stderr="")

    monkeypatch.setattr("sdlc.reconcile.subprocess.run", fake_run)
    assert _gh_pr_state(42, tmp_path) == "MERGED"


def test_gh_pr_state_none_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no PR")

    monkeypatch.setattr("sdlc.reconcile.subprocess.run", fake_run)
    assert _gh_pr_state(42, tmp_path) is None


def test_gh_pr_state_none_when_gh_absent(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise OSError("gh not found")

    monkeypatch.setattr("sdlc.reconcile.subprocess.run", fake_run)
    assert _gh_pr_state(42, tmp_path) is None


# --- _ensure_merge_done branches --------------------------------------------


def test_ensure_merge_done_noop_when_already_done(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "99.2-001", "99", "x", "P1", 1, "g", "", None, "TODO")
    ledger.stage_start(run_id, "99.2-001", "merge", 1)
    ledger.stage_finish(run_id, "99.2-001", "merge", 1, "DONE")

    attempts = ledger.stage_breakdown(run_id)["99.2-001"]
    _ensure_merge_done(ledger, run_id, "99.2-001", attempts)

    assert _merge_done(db, run_id, "99.2-001") == 1  # no duplicate row


def test_ensure_merge_done_promotes_existing_attempt(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "99.2-002", "99", "x", "P1", 1, "g", "", None, "TODO")
    # A merge attempt that started but never reached DONE (e.g. failed/parked).
    ledger.stage_start(run_id, "99.2-002", "merge", 1)
    ledger.stage_finish(run_id, "99.2-002", "merge", 1, "FAILED")

    attempts = ledger.stage_breakdown(run_id)["99.2-002"]
    _ensure_merge_done(ledger, run_id, "99.2-002", attempts)

    # The latest attempt is promoted in place — exactly one DONE, no new attempt.
    assert _merge_done(db, run_id, "99.2-002") == 1
    merge_rows = [a for a in ledger.stage_breakdown(run_id)["99.2-002"] if a["name"] == "merge"]
    assert len(merge_rows) == 1


# --- _compute_terminal classification ---------------------------------------


def test_compute_terminal_classifications() -> None:
    assert _compute_terminal({"a": "DONE", "b": "SKIPPED"}) == "DONE"
    assert _compute_terminal({"a": "DONE", "b": "FAILED"}) == "FAILED"
    assert _compute_terminal({"a": "BLOCKED"}) == "FAILED"
    # Leftover work that is neither failed nor done parks the run.
    assert _compute_terminal({"a": "DONE", "b": "AWAITING_APPROVAL"}) == "NEEDS_ATTENTION"


# --- fetch raising (not just non-zero) still degrades to a skip --------------


def test_fetch_exception_degrades_to_skip(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.2-003", "FAILED", 200)])

    def raising_git(_root, *args):
        raise OSError("git binary unavailable")

    monkeypatch.setattr("sdlc.reconcile._git", raising_git)

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=True)

    assert result.skipped is True
    assert result.reclassified == []
    assert result.run_status_after == "FAILED"
