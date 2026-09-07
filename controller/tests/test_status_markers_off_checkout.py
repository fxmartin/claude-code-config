# ABOUTME: Integration tests for Story 32.1-003 — status markers off the shared checkout.
# ABOUTME: Real git fixtures; the #590 dirty-tree guard must stay satisfied throughout a run.

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.build import Ledger, _record_merge_landing, dirty_tree_paths
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.reconcile import reconcile_run


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    """A repo on branch ``main`` carrying a committed epic markdown file."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    story_dir = root / "docs" / "stories"
    story_dir.mkdir(parents=True)
    (story_dir / "epic-23-sample.md").write_text(
        "##### Story 23.2-001: First\n**Status**: Not started\n\n"
        "##### Story 23.2-002: Second\n**Status**: Not started\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def _story(sid: str, epic_file: Path) -> Story:
    return Story(
        id=sid, title="x", epic_id="epic-23", epic_name="sample",
        epic_file=str(epic_file), priority="Should", points=1, agent_type="merge",
    )


def _merged(sha: str) -> AgentResult:
    return AgentResult(
        agent_type="merge",
        data={"merge_status": "MERGED", "merge_sha": sha, "merged_at": "x"},
        raw="",
    )


def _seed_story(ledger: Ledger, run_id: str, sid: str) -> None:
    ledger.story_upsert(
        run_id, sid, "epic-23", "x", "Should", 1, "merge",
        f"feature/{sid}", None, "IN_PROGRESS",
    )


# ---------------------------------------------------------------------------
# DoD: no writes to the shared checkout during a run (fingerprint before/after)
# ---------------------------------------------------------------------------


def test_merge_landing_keeps_the_shared_checkout_clean(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    epic_file = root / "docs" / "stories" / "epic-23-sample.md"
    assert dirty_tree_paths(root) == []  # fingerprint before

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-23", "serial")
    _seed_story(ledger, run_id, "23.2-001")

    _record_merge_landing(
        "merge", _merged("cafef00d"), ledger, run_id, _story("23.2-001", epic_file), None,
    )

    assert dirty_tree_paths(root) == []  # fingerprint after: unchanged


def test_automatic_reconcile_at_close_out_keeps_the_checkout_clean(tmp_path: Path) -> None:
    """reconcile_run — the automatic close-out path — must not dirty the tree either."""
    root = _init_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature/23.2-001")
    (root / "work.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "work.py")
    _git(root, "commit", "-q", "-m", "feat: work (#23.2-001)")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", "feature/23.2-001")
    assert dirty_tree_paths(root) == []  # fingerprint before

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-23", "serial")
    ledger.set_total(run_id, 1)
    _seed_story(ledger, run_id, "23.2-001")
    ledger.set_story_status(run_id, "23.2-001", "FAILED")  # parked, reconcile-eligible

    result = reconcile_run(ledger, run_id, root=root, fetch=False)

    assert [r["story_id"] for r in result.reclassified] == ["23.2-001"]
    assert dirty_tree_paths(root) == []  # fingerprint after: unchanged


# ---------------------------------------------------------------------------
# DoD: two overlapping build runs in one repo pass the guard throughout
# ---------------------------------------------------------------------------


def test_two_overlapping_build_runs_in_one_repo_never_trip_the_guard(tmp_path: Path) -> None:
    """Two build jobs landing merges for different stories, interleaved, in one repo."""
    root = _init_repo(tmp_path)
    epic_file = root / "docs" / "stories" / "epic-23-sample.md"

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_a = ledger.run_create("epic-23", "serial")
    run_b = ledger.run_create("epic-23", "serial")
    _seed_story(ledger, run_a, "23.2-001")
    _seed_story(ledger, run_b, "23.2-002")

    assert dirty_tree_paths(root) == []

    # Job A's merge lands...
    _record_merge_landing(
        "merge", _merged("aaa"), ledger, run_a, _story("23.2-001", epic_file), None,
    )
    assert dirty_tree_paths(root) == []  # job B's next dirty-tree check sees a clean repo

    # ...then job B's merge lands, interleaved with job A still live.
    _record_merge_landing(
        "merge", _merged("bbb"), ledger, run_b, _story("23.2-002", epic_file), None,
    )
    assert dirty_tree_paths(root) == []

    assert ledger.story_merge_sha(run_a, "23.2-001") == "aaa"
    assert ledger.story_merge_sha(run_b, "23.2-002") == "bbb"
