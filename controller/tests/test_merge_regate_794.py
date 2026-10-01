# ABOUTME: Issue #794 — a merge agent that reports pending checks on a head it
# ABOUTME: pushed itself re-gates and retries the merge; it never enters bugfix.
from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc import build_issue
from sdlc import issue_host as ih
from sdlc.build import (
    BuildOptions,
    Ledger,
    _merge_checks_pending,
    _sync_branch_to_remote,
    render_merge_prompt,
    run_build,
)
from sdlc.cohort import Story

from test_build import FakeDispatcher, _default_payload  # noqa: E402 — sibling fakes


def _story() -> Story:
    return Story("g1-001", "Gate story", "23", "pipeline-on-gitlab",
                 "epic-23.md", "Should", 2, "py", [])


def _pending_payload(story: Story) -> dict:
    return {"pr_number": 100, "merge_status": "FAILED", "merge_sha": "",
            "merged_at": "", "block_reason": "CHECKS_PENDING"}


def _sequence(*payloads):
    it = iter(payloads)
    return lambda: next(it)


def _statuses(monkeypatch, statuses: list[str]) -> list[int]:
    calls: list[int] = []

    def _status(ledger, story_id, cr_ref, *, runner=None):
        calls.append(1)
        return statuses[min(len(calls) - 1, len(statuses) - 1)]

    monkeypatch.setattr(build_issue, "change_request_status", _status)
    return calls


def _events(db: Path, run_id: str) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        )]
    finally:
        conn.close()


# --- classification ----------------------------------------------------------


def test_checks_pending_marker_and_free_text_are_recognised() -> None:
    assert _merge_checks_pending("merge", {"block_reason": "CHECKS_PENDING"})
    assert _merge_checks_pending("merge", {"block_reason": "checks_pending"})
    # Run 60c2947e / 34.4-001: the agent's own words for the same situation.
    assert _merge_checks_pending(
        "merge", {"block_reason": "branch_protection_requires_passing_checks"}
    )
    assert _merge_checks_pending("merge", {"summary": "checks still running on the new head"})


def test_checks_pending_is_merge_only_and_distinct_from_high_risk() -> None:
    assert not _merge_checks_pending("build", {"block_reason": "CHECKS_PENDING"})
    assert not _merge_checks_pending("merge", {"block_reason": "BLOCKED_HIGH_RISK"})
    assert not _merge_checks_pending("merge", {"block_reason": "REBASE_CONFLICT"})
    assert not _merge_checks_pending("merge", {})


def test_merge_prompt_names_the_checks_pending_contract() -> None:
    prompt = render_merge_prompt(_story(), 7, ci_status=ih.CR_SUCCESS)
    assert 'block_reason to "CHECKS_PENDING"' in prompt


# --- the stage loop ----------------------------------------------------------


def test_pending_checks_regate_then_merge_without_bugfix(tmp_path, monkeypatch) -> None:
    """Green gate → merge agent pushes a drift merge and reports CHECKS_PENDING →
    the gate is re-polled on the new head (green) → merge retried → DONE.
    Bugfix is never dispatched."""
    _statuses(monkeypatch, [ih.CR_SUCCESS])
    story = _story()
    dispatcher = FakeDispatcher(overrides={
        ("merge", story.id): _sequence(_pending_payload(story), _default_payload("merge", story)),
    })
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[story], ledger=Ledger(db), dispatcher=dispatcher,
                       preflight=lambda: True)

    assert result.completed == 1
    assert [a for a, _ in dispatcher.calls].count("merge") == 2
    assert ("bugfix", story.id) not in dispatcher.calls
    assert any("merge re-gate" in m for m in _events(db, result.run_id))


def test_pending_checks_then_red_gate_routes_to_bugfix(tmp_path, monkeypatch) -> None:
    """The re-poll is a real gate: a head that goes red routes to bugfix as today."""
    _statuses(monkeypatch, [ih.CR_SUCCESS, ih.CR_FAILED])
    story = _story()
    dispatcher = FakeDispatcher(overrides={
        ("merge", story.id): _sequence(_pending_payload(story)),
    })
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[story], ledger=Ledger(tmp_path / "ledger.db"),
                       dispatcher=dispatcher, preflight=lambda: True)

    assert [a for a, _ in dispatcher.calls].count("merge") == 1
    assert ("bugfix", story.id) in dispatcher.calls
    assert result.completed == 0


def test_regate_happens_at_most_once_per_merge_stage(tmp_path, monkeypatch) -> None:
    """Two CHECKS_PENDING reports in a row: the second is a plain failure (the
    agent is not making progress), so the loop cannot spin on re-gates."""
    _statuses(monkeypatch, [ih.CR_SUCCESS])
    story = _story()
    dispatcher = FakeDispatcher(overrides={
        ("merge", story.id): _sequence(_pending_payload(story), _pending_payload(story),
                                       _default_payload("merge", story)),
    })
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    run_build(opts, queue=[story], ledger=Ledger(db), dispatcher=dispatcher,
              preflight=lambda: True)

    calls = [a for a, _ in dispatcher.calls]
    assert calls.count("merge") >= 2
    assert ("bugfix", story.id) in dispatcher.calls
    assert sum("merge re-gate" in m for m in _events(db, _run_id(db))) == 1


def _run_id(db: Path) -> str:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT id FROM runs").fetchone()[0]
    finally:
        conn.close()


# --- the fix pipeline takes the same route ----------------------------------


def test_fix_pipeline_regates_pending_checks_then_merges(tmp_path, monkeypatch) -> None:
    from test_fix_issue import RecordingDispatcher, _default_payload as _fix_default, _run_gated_fix, _stub_cr_status

    _stub_cr_status(monkeypatch, [ih.CR_SUCCESS])
    merge_payloads = iter([
        {"pr_number": 100, "merge_status": "FAILED", "merge_sha": "", "merged_at": "",
         "block_reason": "CHECKS_PENDING"},
        None,  # default MERGED payload
    ])

    def _merge(n):
        payload = next(merge_payloads)
        return payload if payload is not None else _fix_default("merge")

    dispatch = RecordingDispatcher(overrides={"merge": _merge})
    result = _run_gated_fix(tmp_path, dispatch)
    assert result.status == "DONE"
    assert dispatch.counts["merge"] == 2
    assert "bugfix" not in dispatch.agents()


# --- the bugfix push can never be non-fast-forward ---------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare origin, a 'merge agent' clone, and a 'bugfix worktree' clone."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    clones = []
    for name in ("agent", "bugfix"):
        c = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True)
        _git(c, "config", "user.email", "t@example.com")
        _git(c, "config", "user.name", "Test")
        clones.append(c)
    agent, bugfix = clones
    (agent / "README").write_text("base\n")
    _git(agent, "add", "-A"); _git(agent, "commit", "-q", "-m", "chore: base")
    _git(agent, "push", "-q", "origin", "main")
    _git(agent, "checkout", "-q", "-b", "feature/g1-001")
    (agent / "a.txt").write_text("a\n")
    _git(agent, "add", "-A"); _git(agent, "commit", "-q", "-m", "feat: a (#g1-001)")
    _git(agent, "push", "-q", "-u", "origin", "feature/g1-001")
    _git(bugfix, "fetch", "-q", "origin")
    _git(bugfix, "checkout", "-q", "-b", "feature/g1-001", "origin/feature/g1-001")
    return origin, agent, bugfix


def test_sync_fast_forwards_a_branch_the_merge_agent_advanced(tmp_path) -> None:
    """The 34.4-001 shape: the merge agent pushed a drift merge; the bugfix
    worktree still sits on the older head. Sync fast-forwards it."""
    _origin, agent, bugfix = _repo_pair(tmp_path)
    (agent / "b.txt").write_text("drift merge\n")
    _git(agent, "add", "-A"); _git(agent, "commit", "-q", "-m", "merge: absorb main")
    _git(agent, "push", "-q", "origin", "feature/g1-001")
    remote_head = _git(agent, "rev-parse", "HEAD")

    assert _sync_branch_to_remote(bugfix, "feature/g1-001") is True
    assert _git(bugfix, "rev-parse", "HEAD") == remote_head
    assert (bugfix / "b.txt").exists()


def test_sync_leaves_a_diverged_branch_alone(tmp_path) -> None:
    _origin, agent, bugfix = _repo_pair(tmp_path)
    (agent / "b.txt").write_text("remote\n")
    _git(agent, "add", "-A"); _git(agent, "commit", "-q", "-m", "remote commit")
    _git(agent, "push", "-q", "origin", "feature/g1-001")
    (bugfix / "c.txt").write_text("local\n")
    _git(bugfix, "add", "-A"); _git(bugfix, "commit", "-q", "-m", "local commit")
    local_head = _git(bugfix, "rev-parse", "HEAD")

    assert _sync_branch_to_remote(bugfix, "feature/g1-001") is False
    assert _git(bugfix, "rev-parse", "HEAD") == local_head  # nothing rewritten


def test_sync_is_a_noop_when_already_current(tmp_path) -> None:
    _origin, _agent, bugfix = _repo_pair(tmp_path)
    head = _git(bugfix, "rev-parse", "HEAD")
    assert _sync_branch_to_remote(bugfix, "feature/g1-001") is True
    assert _git(bugfix, "rev-parse", "HEAD") == head


def test_sync_tolerates_a_missing_remote_branch(tmp_path) -> None:
    _origin, _agent, bugfix = _repo_pair(tmp_path)
    assert _sync_branch_to_remote(bugfix, "feature/never-pushed") is False
