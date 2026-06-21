# ABOUTME: Tests for close-out reconciliation against origin/main (Story 12.3-001).
# ABOUTME: Builds real temp git repos, parks landed stories, asserts reclassification.

from __future__ import annotations

import subprocess
from pathlib import Path

import sdlc.reconcile as reconcile_mod
from sdlc.build import Ledger
from sdlc.reconcile import ReconcileResult, _gh_pr_state, reconcile_run


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


def _stage_done(db_path: Path, run_id: str, story_id: str, stage: str) -> int:
    rows = Ledger(db_path).stage_breakdown(run_id).get(story_id, [])
    return sum(1 for a in rows if a["name"] == stage and a["status"] == "DONE")


def _pr_number(db_path: Path, run_id: str, story_id: str) -> int | None:
    return {r["story_id"]: r["pr_number"] for r in Ledger(db_path).story_rows(run_id)}[
        story_id
    ]


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


# --- legitimate fast-forward landing (≥1 own commit) still fires (#111) ------


def test_fast_forward_with_own_commit_still_lands(tmp_path: Path) -> None:
    """The real is-ancestor positive (branch ahead by ≥1 then merged) survives."""
    from sdlc.reconcile import _detect_landing

    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-104", new=True)
    sha = _commit(root, "ff2.py", "v = 1\n", "feat: ff2 (#99.1-104)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-104")
    base = _git(root, "rev-parse", "HEAD").stdout.strip()

    landing = _detect_landing("99.1-104", None, base, root)
    assert landing is not None
    assert landing[0] == "is-ancestor"
    assert landing[1] == sha


# --- empty stacked branch must NOT false-positive as a landing (#111) --------


def test_empty_stacked_branch_does_not_falsely_land(tmp_path: Path) -> None:
    """An empty story branch (0 commits ahead of base) must NOT report a landing.

    ``git merge-base --is-ancestor <branch> <base>`` is trivially true for a
    branch carrying none of its own work (e.g. a story cut stacked on a sibling
    whose work later merged to main, while the story itself was never built).
    Without a "ahead by ≥1 commit" floor, reconcile would mark a never-built
    story DONE with no PR and no code (#111). It must return None instead.
    """
    from sdlc.reconcile import _detect_landing

    root = _init_repo(tmp_path)
    # A sibling's work lands on main.
    _commit(root, "sibling.py", "s = 1\n", "feat: sibling landed (#99.1-100)")
    # The story branch is cut from main and carries zero own commits — its tip
    # is therefore (trivially) an ancestor of main.
    _checkout(root, "feature/99.1-101", new=True)
    _checkout(root, "main")

    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert _detect_landing("99.1-101", None, base, root) is None


def test_empty_stacked_branch_stays_parked_end_to_end(tmp_path: Path) -> None:
    """End-to-end: a parked story with an empty branch is not flipped to DONE."""
    root = _init_repo(tmp_path)
    _commit(root, "sibling.py", "s = 1\n", "feat: sibling landed (#99.1-102)")
    _checkout(root, "feature/99.1-103", new=True)
    _checkout(root, "main")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-103", "FAILED", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert _status(db, run_id, "99.1-103") == "FAILED"
    assert result.run_status_after == "FAILED"
    assert _merge_done(db, run_id, "99.1-103") == 0


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


# --- _gh_pr_state: real body across gh outcomes -----------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_gh_pr_state_merged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        reconcile_mod.subprocess, "run", lambda *a, **k: _FakeProc(0, "MERGED\n")
    )
    assert _gh_pr_state(200, tmp_path) == "MERGED"


def test_gh_pr_state_nonzero_returns_none(tmp_path: Path, monkeypatch) -> None:
    # gh present but the call fails (unauthenticated / unknown PR) → no signal.
    monkeypatch.setattr(
        reconcile_mod.subprocess, "run", lambda *a, **k: _FakeProc(1, "boom")
    )
    assert _gh_pr_state(201, tmp_path) is None


def test_gh_pr_state_empty_stdout_returns_none(tmp_path: Path, monkeypatch) -> None:
    # Exit 0 with blank state → `strip() or None` yields None, not "".
    monkeypatch.setattr(
        reconcile_mod.subprocess, "run", lambda *a, **k: _FakeProc(0, "  \n")
    )
    assert _gh_pr_state(202, tmp_path) is None


def test_gh_pr_state_subprocess_error_returns_none(tmp_path: Path, monkeypatch) -> None:
    # gh absent / spawn failure must degrade silently, never raise.
    def _raise(*_a, **_k):
        raise OSError("gh not found")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", _raise)
    assert _gh_pr_state(203, tmp_path) is None


# --- _ensure_merge_done: existing DONE merge is not duplicated --------------


def test_existing_done_merge_not_duplicated(tmp_path: Path) -> None:
    # A parked-but-already-has-a-DONE-merge story: reconciliation must flip it to
    # DONE without recording a second merge row.
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-010", new=True)
    _commit(root, "j.py", "j = 1\n", "feat: j (#99.1-010)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-010")

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "99.1-010", "99", "j", "P1", 1, "general-purpose", "", None, "TODO"
    )
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-010", stage, 1)
        ledger.stage_finish(run_id, "99.1-010", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-010", 110)
    ledger.set_story_status(run_id, "99.1-010", "FAILED")  # parked despite DONE merge
    ledger.run_update_status(run_id, "FAILED")

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-010"]
    assert _status(db, run_id, "99.1-010") == "DONE"
    assert _merge_done(db, run_id, "99.1-010") == 1  # not duplicated


# --- _ensure_merge_done: promote a non-DONE merge attempt -------------------


def test_promotes_non_done_merge_attempt(tmp_path: Path) -> None:
    # A parked story whose merge attempt exists but FAILED: reconciliation must
    # promote that attempt to DONE rather than synthesize a new one.
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-011", new=True)
    _commit(root, "k.py", "k = 1\n", "feat: k (#99.1-011)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-011")

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "99.1-011", "99", "k", "P1", 1, "general-purpose", "", None, "TODO"
    )
    for stage in ("build", "review"):
        ledger.stage_start(run_id, "99.1-011", stage, 1)
        ledger.stage_finish(run_id, "99.1-011", stage, 1, "DONE")
    ledger.stage_start(run_id, "99.1-011", "merge", 1)
    ledger.stage_finish(run_id, "99.1-011", "merge", 1, "FAILED")
    ledger.set_story_pr(run_id, "99.1-011", 111)
    ledger.set_story_status(run_id, "99.1-011", "FAILED")
    ledger.run_update_status(run_id, "FAILED")

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-011"]
    assert result.changed is True
    assert _merge_done(db, run_id, "99.1-011") == 1  # FAILED attempt promoted


# --- _compute_terminal: unlanded NEEDS_ATTENTION leaves run NEEDS_ATTENTION --


def test_terminal_needs_attention_when_unlanded_remains(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)  # no feature branch → nothing landed
    _checkout(root, "feature/99.1-012", new=True)
    _commit(root, "wip.py", "w = 1\n", "feat: wip (#99.1-012)")
    _checkout(root, "main")  # never merged

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-012", "NEEDS_ATTENTION", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert result.run_status_after == "NEEDS_ATTENTION"
    assert _status(db, run_id, "99.1-012") == "NEEDS_ATTENTION"


# --- fetch raising (not just non-zero) degrades to a skip -------------------


def test_fetch_exception_degrades_to_skip(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-013", new=True)
    _commit(root, "x.py", "x = 1\n", "feat: x (#99.1-013)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-013")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-013", "FAILED", 113)])

    real_git = reconcile_mod._git

    def _git_or_raise(root_, *args):
        if args[:1] == ("fetch",):
            raise OSError("git unavailable")
        return real_git(root_, *args)

    monkeypatch.setattr(reconcile_mod, "_git", _git_or_raise)

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=True)

    assert result.skipped is True
    assert result.reclassified == []
    assert _status(db, run_id, "99.1-013") == "FAILED"


# --- AWAITING_APPROVAL: approve-then-merge → DONE; un-landed stays awaiting ---
# (Story 12.3-003)


def test_awaiting_approval_landed_reconciles_to_done(tmp_path: Path) -> None:
    """A high-risk-blocked story FX later approves and merges reconciles to DONE."""
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-020", new=True)
    _commit(root, "approved.py", "z = 3\n", "feat: approved (#99.1-020)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-020")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-020", "AWAITING_APPROVAL", 120)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-020"]
    assert _status(db, run_id, "99.1-020") == "DONE"
    assert _merge_done(db, run_id, "99.1-020") == 1
    # All stories now DONE → the run terminal recomputes to DONE.
    assert result.run_status_after == "DONE"


def test_awaiting_approval_unlanded_keeps_awaiting_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    """A standalone reconcile over a not-yet-approved run keeps AWAITING_APPROVAL.

    Reconciliation must never downgrade the honest awaiting-human signal to
    NEEDS_ATTENTION (and never to FAILED) just because the PR has not merged yet.
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-021", new=True)
    _commit(root, "pending.py", "p = 4\n", "feat: pending (#99.1-021)")
    _checkout(root, "main")  # work is NOT on main
    monkeypatch.setattr("sdlc.reconcile._gh_pr_state", lambda pr_number, root: None)

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-021", "AWAITING_APPROVAL", 121)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert _status(db, run_id, "99.1-021") == "AWAITING_APPROVAL"
    assert result.run_status_after == "AWAITING_APPROVAL"


def test_compute_terminal_awaiting_approval_precedence() -> None:
    from sdlc.reconcile import _compute_terminal

    # Pure awaiting → AWAITING_APPROVAL.
    assert _compute_terminal({"a": "DONE", "b": "AWAITING_APPROVAL"}) == "AWAITING_APPROVAL"
    # Mixed awaiting + needs-attention → NEEDS_ATTENTION (stuck work wins).
    assert (
        _compute_terminal({"a": "AWAITING_APPROVAL", "b": "NEEDS_ATTENTION"})
        == "NEEDS_ATTENTION"
    )
    # A failed story still dominates.
    assert _compute_terminal({"a": "AWAITING_APPROVAL", "b": "FAILED"}) == "FAILED"


# --- #105: ledger left cosmetically incomplete after recovery ---------------


def test_reconcile_terminalizes_intermediate_stages(tmp_path: Path) -> None:
    """A landed-but-parked story has its build/coverage/review stages DONE.

    The seed records DONE build+review but never coverage and never merge.
    After reconcile flips the story to DONE, every non-merge pipeline stage must
    carry a DONE attempt (the dashboard otherwise renders coverage as PENDING on
    an otherwise-DONE story — #105).
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-030", new=True)
    _commit(root, "s.py", "s = 1\n", "feat: s (#99.1-030)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-030")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-030", "FAILED", 130)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-030"]
    assert _status(db, run_id, "99.1-030") == "DONE"
    # coverage was never recorded by the seed → reconcile must synthesize a DONE.
    assert _stage_done(db, run_id, "99.1-030", "coverage") == 1
    # build / review were already DONE — still DONE, not duplicated.
    assert _stage_done(db, run_id, "99.1-030", "build") == 1
    assert _stage_done(db, run_id, "99.1-030", "review") == 1
    assert _merge_done(db, run_id, "99.1-030") == 1
    # The synthesized coverage row is marked with the reconcile output_path.
    rows = Ledger(db).stage_breakdown(run_id)["99.1-030"]
    cov = [a for a in rows if a["name"] == "coverage"]
    assert cov and cov[0]["output_path"] == "reconcile"


def test_reconcile_backfills_pr_via_gh_when_absent(tmp_path: Path, monkeypatch) -> None:
    """A landing detected via is-ancestor (no PR on file) backfills the PR via gh.

    The seed parks the story with pr_number=None. reconcile must resolve the PR
    behind the landing sha through gh and persist it on the story row (#105).
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-031", new=True)
    _commit(root, "t.py", "t = 1\n", "feat: t (#99.1-031)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-031")

    monkeypatch.setattr(
        "sdlc.reconcile._gh_pr_for_landing",
        lambda story_id, sha, root: 131,
    )

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-031", "FAILED", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-031"]
    assert _pr_number(db, run_id, "99.1-031") == 131


def test_reconcile_keeps_existing_pr_number(tmp_path: Path, monkeypatch) -> None:
    """A story already carrying a PR keeps it; no gh lookup is needed."""
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-032", new=True)
    _commit(root, "u.py", "u = 1\n", "feat: u (#99.1-032)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-032")

    # If gh were consulted it would return a different number; assert it is not.
    monkeypatch.setattr(
        "sdlc.reconcile._gh_pr_for_landing",
        lambda story_id, sha, root: 999,
    )

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-032", "FAILED", 132)])

    reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert _pr_number(db, run_id, "99.1-032") == 132


def test_reconcile_pr_backfill_offline_safe(tmp_path: Path, monkeypatch) -> None:
    """gh lookup failing (offline / gh absent) leaves pr_number null, no crash.

    The story still reconciles to DONE and its stages are still terminalized —
    PR backfill is strictly best-effort (#105).
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-033", new=True)
    _commit(root, "v.py", "v = 1\n", "feat: v (#99.1-033)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-033")

    monkeypatch.setattr(
        "sdlc.reconcile._gh_pr_for_landing",
        lambda story_id, sha, root: None,
    )

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-033", "FAILED", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-033"]
    assert _pr_number(db, run_id, "99.1-033") is None
    assert _status(db, run_id, "99.1-033") == "DONE"
    assert _stage_done(db, run_id, "99.1-033", "coverage") == 1


def test_reconcile_stage_terminalization_idempotent(tmp_path: Path) -> None:
    """Re-running reconcile adds no duplicate stage rows and does not re-flip."""
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-034", new=True)
    _commit(root, "w.py", "w = 1\n", "feat: w (#99.1-034)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-034")

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-034", "FAILED", 134)])

    first = reconcile_run(Ledger(db), run_id, root=root, fetch=False)
    assert len(first.reclassified) == 1
    assert _stage_done(db, run_id, "99.1-034", "coverage") == 1

    second = reconcile_run(Ledger(db), run_id, root=root, fetch=False)
    assert second.reclassified == []
    # Exactly one DONE coverage attempt after the re-run — no duplication.
    assert _stage_done(db, run_id, "99.1-034", "coverage") == 1
    assert _stage_done(db, run_id, "99.1-034", "build") == 1
    assert _merge_done(db, run_id, "99.1-034") == 1


def test_unlanded_story_stages_not_terminalized(tmp_path: Path, monkeypatch) -> None:
    """A non-landed parked story is untouched: no PR backfill, no stage DONE.

    Guards the #111 contract — an unlanded story must stay parked, and the #105
    terminalization must never fire on it (no coverage DONE synthesized).
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-035", new=True)
    _commit(root, "wip.py", "z = 3\n", "feat: wip (#99.1-035)")
    _checkout(root, "main")  # never merged
    monkeypatch.setattr("sdlc.reconcile._gh_pr_state", lambda pr_number, root: None)
    monkeypatch.setattr(
        "sdlc.reconcile._gh_pr_for_landing", lambda story_id, sha, root: 935
    )

    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-035", "FAILED", None)])

    result = reconcile_run(Ledger(db), run_id, root=root, fetch=False)

    assert result.reclassified == []
    assert _status(db, run_id, "99.1-035") == "FAILED"
    assert _pr_number(db, run_id, "99.1-035") is None  # no backfill on a non-landing
    assert _stage_done(db, run_id, "99.1-035", "coverage") == 0


# --- _gh_pr_for_landing: direct unit coverage of error paths ----------------


def test_gh_pr_for_landing_subprocess_error_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    """OSError/SubprocessError in gh call returns None without raising (lines 92-93)."""
    from sdlc.reconcile import _gh_pr_for_landing

    def _raise(*_a, **_k):
        raise OSError("gh not found")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", _raise)
    assert _gh_pr_for_landing("99.1-040", "abc1234", tmp_path) is None


def test_gh_pr_for_landing_nonzero_continues_to_head_ref(
    tmp_path: Path, monkeypatch
) -> None:
    """First query returns non-zero; the second succeeds — PR resolved (lines 94-95)."""
    from sdlc.reconcile import _gh_pr_for_landing

    call_count = [0]

    def _fake_run(*_a, **_k):
        call_count[0] += 1
        # First call (head: ref query) → fail; second call (sha fallback) → success.
        if call_count[0] == 1:
            return _FakeProc(1, "")
        return _FakeProc(0, "142\n")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", _fake_run)
    result = _gh_pr_for_landing("99.1-041", "deadbeef", tmp_path)
    assert result == 142
    assert call_count[0] == 2


def test_gh_pr_for_landing_prefers_head_ref_over_sha(
    tmp_path: Path, monkeypatch
) -> None:
    """The story-anchored head: ref is tried first, shadowing the sha fallback.

    Guards against attaching a sibling's PR: a git-cherry / gh-pr-merged landing
    hands us the *base tip* sha, whose associated PR may belong to another story.
    The head: ref query resolves this story's own PR first, so the sha fallback
    (which here would return a sibling's 777) is never consulted.
    """
    from sdlc.reconcile import _gh_pr_for_landing

    seen_queries: list[str] = []

    def _fake_run(cmd, *_a, **_k):
        query = cmd[cmd.index("--search") + 1]
        seen_queries.append(query)
        if query.startswith("head:"):
            return _FakeProc(0, "555\n")
        return _FakeProc(0, "777\n")  # sibling PR behind the base-tip sha

    monkeypatch.setattr(reconcile_mod.subprocess, "run", _fake_run)
    result = _gh_pr_for_landing("99.1-044", "basetipsha", tmp_path)
    assert result == 555
    assert seen_queries == ["head:feature/99.1-044"]  # sha never queried


def test_gh_pr_for_landing_invalid_number_continues_and_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    """Non-integer stdout on all queries triggers ValueError continue → None (lines 98-101)."""
    from sdlc.reconcile import _gh_pr_for_landing

    monkeypatch.setattr(
        reconcile_mod.subprocess, "run", lambda *_a, **_k: _FakeProc(0, "not-a-number\n")
    )
    assert _gh_pr_for_landing("99.1-042", "abc1234", tmp_path) is None


# --- _ensure_stages_done: promote a non-DONE existing attempt ---------------


def test_ensure_stages_done_promotes_non_done_attempt(tmp_path: Path) -> None:
    """A stage with an existing FAILED attempt is promoted to DONE (lines 239-240).

    Seeds build=FAILED (no DONE) and coverage/review absent.  After reconcile
    the build attempt must be promoted (not synthesized fresh) — confirming the
    ``if stage_attempts:`` branch in ``_ensure_stages_done`` fires.
    """
    root = _init_repo(tmp_path)
    _checkout(root, "feature/99.1-043", new=True)
    _commit(root, "promote.py", "p = 1\n", "feat: promote (#99.1-043)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-043")

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "99.1-043", "99", "promote", "P1", 1, "general-purpose", "", None, "TODO"
    )
    # Record a FAILED build attempt — exists but not DONE → _ensure_stages_done
    # must promote it rather than synthesize a fresh one (lines 239-240).
    ledger.stage_start(run_id, "99.1-043", "build", 1)
    ledger.stage_finish(run_id, "99.1-043", "build", 1, "FAILED")
    ledger.set_story_status(run_id, "99.1-043", "FAILED")
    ledger.run_update_status(run_id, "FAILED")

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["99.1-043"]
    assert _status(db, run_id, "99.1-043") == "DONE"
    # build: FAILED attempt was promoted to DONE — exactly one DONE row.
    assert _stage_done(db, run_id, "99.1-043", "build") == 1
    # coverage / review had no prior attempts → synthesized fresh.
    assert _stage_done(db, run_id, "99.1-043", "coverage") == 1
    assert _stage_done(db, run_id, "99.1-043", "review") == 1
    assert _merge_done(db, run_id, "99.1-043") == 1
