# ABOUTME: Tests for the primary-checkout escape detector (issue #607).
# ABOUTME: Real temp git repos for the fingerprint; a real ledger for the event.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sdlc.build import (
    Ledger,
    check_primary_checkout_unchanged,
    guard_primary_checkout,
    primary_checkout_fingerprint,
)


# --- git fixture helpers ----------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A repo on ``main`` with one committed file. No remote, no hooks."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "CLAUDE.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


# ---------------------------------------------------------------------------
# The fingerprint: what counts as "the operator's checkout moved"
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_when_nothing_changes(tmp_path) -> None:
    root = _init_repo(tmp_path)
    assert primary_checkout_fingerprint(root) == primary_checkout_fingerprint(root)


def test_fingerprint_moves_when_a_tracked_file_is_modified(tmp_path) -> None:
    """The #607 contamination half: the agent writes story files into the root."""
    root = _init_repo(tmp_path)
    before = primary_checkout_fingerprint(root)
    (root / "CLAUDE.md").write_text("agent wrote here\n", encoding="utf-8")
    assert primary_checkout_fingerprint(root) != before


def test_fingerprint_moves_when_an_untracked_file_appears(tmp_path) -> None:
    """Observed live: `result_schema.py` / `test_result_schema.py` landed untracked.

    Deliberately unlike ``dirty_tree_paths`` (which ignores untracked scratch):
    an agent depositing *new* files in the operator's tree is exactly the escape
    this detector exists to catch.
    """
    root = _init_repo(tmp_path)
    before = primary_checkout_fingerprint(root)
    (root / "result_schema.py").write_text("x = 1\n", encoding="utf-8")
    assert primary_checkout_fingerprint(root) != before


def test_fingerprint_moves_when_operator_work_is_reverted(tmp_path) -> None:
    """The dangerous half: an agent-initiated `git restore` in the operator's tree.

    The operator's uncommitted edit is present at story start and gone at story
    end — the fingerprint must move, because this is the data-loss case.
    """
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("operator's uncommitted work\n", encoding="utf-8")
    before = primary_checkout_fingerprint(root)
    _git(root, "restore", "CLAUDE.md")  # the agent "correcting" itself
    assert primary_checkout_fingerprint(root) != before


def test_fingerprint_moves_on_a_content_edit_to_an_already_dirty_file(tmp_path) -> None:
    """A path-set comparison alone would miss this; the digest covers content."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("operator edit\n", encoding="utf-8")
    before = primary_checkout_fingerprint(root)
    (root / "CLAUDE.md").write_text("agent overwrote it\n", encoding="utf-8")
    assert primary_checkout_fingerprint(root) != before


def test_fingerprint_ignores_the_worktrees_dir(tmp_path) -> None:
    """The agent's own worktree lives under the root and must not self-trip."""
    root = _init_repo(tmp_path)
    before = primary_checkout_fingerprint(root)
    wt = root / ".claude" / "worktrees" / "agent-abc123-05.1-001"
    wt.mkdir(parents=True)
    (wt / "runner.py").write_text("legitimate in-worktree work\n", encoding="utf-8")
    assert primary_checkout_fingerprint(root) == before


def test_fingerprint_degrades_to_none_outside_a_repo(tmp_path) -> None:
    """An un-inspectable tree disables the check rather than failing every story."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert primary_checkout_fingerprint(plain) is None


# ---------------------------------------------------------------------------
# The assertion: a silent escape becomes a loud, surfaced event
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "state.db")
    ledger.init()
    run_id = ledger.run_create("epic-05", "parallel")
    return ledger, run_id


def _events(ledger: Ledger, run_id: str) -> list[tuple[str, str]]:
    return [(e["level"], e["message"]) for e in ledger.recent_events(run_id)]


def test_unchanged_checkout_logs_nothing_and_reports_clean(tmp_path) -> None:
    root = _init_repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    before = primary_checkout_fingerprint(root)

    assert check_primary_checkout_unchanged(
        ledger, run_id, "05.1-001", root, before
    ) is True
    assert _events(ledger, run_id) == []


def test_contaminated_checkout_logs_an_error_naming_the_paths(tmp_path) -> None:
    root = _init_repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    before = primary_checkout_fingerprint(root)
    (root / "CLAUDE.md").write_text("agent wrote here\n", encoding="utf-8")
    (root / "result_schema.py").write_text("x = 1\n", encoding="utf-8")

    assert check_primary_checkout_unchanged(
        ledger, run_id, "05.1-001", root, before
    ) is False

    events = _events(ledger, run_id)
    assert len(events) == 1
    level, message = events[0]
    # `error`, not `progress` — #607's complaint was that nothing surfaced above
    # progress, so `sdlc status` showed no warning at all.
    assert level == "error"
    assert "CLAUDE.md" in message
    assert "result_schema.py" in message


def test_a_reverted_checkout_is_called_out_as_discarded_work(tmp_path) -> None:
    """The data-loss direction: the tree moved *and* is now clean.

    Naming this case matters — nothing is dirty to list, so a generic "changed"
    message would read as "no paths reported" for the single most destructive
    outcome the issue describes.
    """
    root = _init_repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    (root / "CLAUDE.md").write_text("operator's uncommitted work\n", encoding="utf-8")
    before = primary_checkout_fingerprint(root)
    _git(root, "restore", "CLAUDE.md")  # the agent "correcting" itself

    assert check_primary_checkout_unchanged(
        ledger, run_id, "05.1-001", root, before
    ) is False

    level, message = _events(ledger, run_id)[0]
    assert level == "error"
    assert "reverted or discarded" in message
    assert "no paths reported" not in message


def test_the_detector_never_reverts_the_checkout(tmp_path) -> None:
    """#607 expectation 2: quarantine, never auto-revert someone else's tree."""
    root = _init_repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    before = primary_checkout_fingerprint(root)
    (root / "CLAUDE.md").write_text("agent wrote here\n", encoding="utf-8")

    check_primary_checkout_unchanged(ledger, run_id, "05.1-001", root, before)

    # The contamination is still on disk — the operator decides what happens to
    # it. An agent-initiated restore is the data-loss step this issue is about.
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "agent wrote here\n"


def test_a_none_baseline_skips_the_check(tmp_path) -> None:
    """No baseline (un-inspectable tree at story start) → no false accusation."""
    root = _init_repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    (root / "CLAUDE.md").write_text("changed\n", encoding="utf-8")

    assert check_primary_checkout_unchanged(
        ledger, run_id, "05.1-001", root, None
    ) is True
    assert _events(ledger, run_id) == []


# ---------------------------------------------------------------------------
# The bracket: how a story is actually wrapped
#
# `_run_one`'s real path can't be exercised end-to-end here — injecting a fake
# dispatcher makes `_prepare_story_workdir` return None, so the guard would
# never arm. The bracket is therefore its own seam, tested directly.
# ---------------------------------------------------------------------------


def test_bracket_returns_the_story_outcome_untouched(tmp_path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)
    ledger, run_id = _ledger(tmp_path)

    result = guard_primary_checkout(
        ledger, run_id, "05.1-001", root / "wt", lambda: "DONE"
    )

    assert result == "DONE"
    assert _events(ledger, run_id) == []


def test_bracket_skips_a_shared_root_story(tmp_path, monkeypatch) -> None:
    """`workdir=None` means the story legitimately owns the root — no bracket.

    Without this, every --sequential build would report itself as an escape.
    """
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)
    ledger, run_id = _ledger(tmp_path)

    def _edit_the_root() -> str:
        (root / "CLAUDE.md").write_text("legitimate shared-root work\n", encoding="utf-8")
        return "DONE"

    assert guard_primary_checkout(
        ledger, run_id, "05.1-001", None, _edit_the_root
    ) == "DONE"
    assert _events(ledger, run_id) == []


def test_bracket_flags_an_isolated_story_that_touched_the_root(
    tmp_path, monkeypatch
) -> None:
    """The #607 scenario, end to end through the seam the scheduler calls."""
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)
    ledger, run_id = _ledger(tmp_path)

    def _escape() -> str:
        (root / "CLAUDE.md").write_text("agent escaped its worktree\n", encoding="utf-8")
        return "DONE"

    assert guard_primary_checkout(
        ledger, run_id, "05.1-001", root / "wt", _escape
    ) == "DONE"

    events = _events(ledger, run_id)
    assert [level for level, _ in events] == ["error"]
    assert "CLAUDE.md" in events[0][1]


def test_bracket_still_checks_when_the_story_raises(tmp_path, monkeypatch) -> None:
    """A crashed story is *more* likely to have left the operator's tree dirty."""
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)
    ledger, run_id = _ledger(tmp_path)

    def _escape_then_die() -> str:
        (root / "CLAUDE.md").write_text("half-written\n", encoding="utf-8")
        raise RuntimeError("stage blew up")

    with pytest.raises(RuntimeError, match="stage blew up"):
        guard_primary_checkout(ledger, run_id, "05.1-001", root / "wt", _escape_then_die)

    events = _events(ledger, run_id)
    assert [level for level, _ in events] == ["error"]
    assert "CLAUDE.md" in events[0][1]
