# ABOUTME: Tests for story 35.5-007 — a merge bugfix round that only re-ran CI re-checks the gate.
# ABOUTME: Real temp git repos + a bare origin; the CI gate and CR-checks seams are faked.

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc import build as build_mod
from sdlc.build import _GATE_BLOCK, _GATE_PASS, BuildOptions, Ledger, _MergeCIGate
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.issue_host import ChangeRequestChecks

SID = "35.5-007"
BRANCH = f"feature/{SID}"
GATE_CHECK = "High-risk file approval gate"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """A clone with CI config and a bare ``origin``; ``feature/<id>`` pushed and tracking."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README").write_text("base\n")
    (work / ".gitlab-ci.yml").write_text("test:\n  script: [true]\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: base")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", BRANCH)
    _git(work, "push", "-u", "origin", BRANCH)
    return work


def _commit(work: Path, name: str) -> None:
    (work / name).write_text(name)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"fix: {name}")


def _story() -> Story:
    return Story(SID, "CI re-run regate", "35", "fleet-execution", "epic-35.md", "Should", 3, "py", [])


_PAYLOADS = {
    "build": {"branch_name": BRANCH, "build_status": "SUCCESS", "commit_sha": "deadbeef"},
    "coverage": {
        "pr_number": 100, "pr_url": "https://example/pull/100", "coverage_pct": 95.0,
        "tests_added": 3, "coverage_status": "PASS", "security_status": "PASS",
    },
    "review": {
        "pr_number": 100, "approval_status": "APPROVED", "change_count": 0,
        "final_status": "APPROVED",
    },
    "merge": {
        "pr_number": 100, "merge_status": "MERGED", "merge_sha": "cafef00d",
        "merged_at": "2026-10-28T00:00:00Z",
    },
}

# What the bugfix agent reports after re-running a red/cancelled job: nothing
# to commit, and the agent cannot see its own re-run's result locally.
_RERAN_CI = {
    "failure_category": "CI_FLAKE",
    "root_cause": "runner shortage cancelled the smoke job; re-ran it",
    "fix_status": "FIXED",
    "tests_passing": False,
    "bugs_fixed": 0,
    "tests_fixed": 0,
}


def _view(*checks: tuple[str, str], high_risk: bool = True) -> ChangeRequestChecks:
    return ChangeRequestChecks(
        labels=("risk:high",) if high_risk else (), checks=tuple(checks)
    )


def _run(work, tmp_path, monkeypatch, gate_statuses, *, views=(), commit=False):
    """Drive run_build to a merge whose first CI gate blocks, then one bugfix per round."""
    monkeypatch.chdir(work)
    statuses = list(gate_statuses)
    gate_calls: list[str] = []

    def gate(stage, ledger, run_id, story, pr, opts, **kw):
        if stage != "merge":
            return None
        status = statuses.pop(0) if statuses else build_mod.CR_SUCCESS
        gate_calls.append(status)
        verdict = _GATE_PASS if status == build_mod.CR_SUCCESS else _GATE_BLOCK
        return _MergeCIGate(
            verdict=verdict, status=status, reason=f"pipeline {status}",
            polls=1, waited_s=0.0, ci_configured=True,
        )

    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)
    pending_views = list(views)

    def checks(ledger, story_id, cr_ref, **kw):
        if not pending_views:
            return None
        return pending_views.pop(0) if len(pending_views) > 1 else pending_views[0]

    monkeypatch.setattr(build_mod.build_issue, "change_request_checks", checks)
    seen: dict[str, list[str]] = {"bugfix": [], "merge": []}

    def dispatch(agent_type, prompt, story=None, **kw):
        if agent_type in seen:
            seen[agent_type].append(prompt)
        if agent_type == "bugfix":
            if commit:
                _commit(work, f"fix{len(seen['bugfix'])}.txt")
            return AgentResult(agent_type, dict(_RERAN_CI), "")
        return AgentResult(agent_type, _PAYLOADS[agent_type], "")

    ledger = Ledger(tmp_path / "loop.db")
    result = build_mod.run_build(
        BuildOptions(scope="epic-35", skip_preflight=True, sequential=True, auto=True),
        queue=[_story()], ledger=ledger,
        dispatcher=dispatch, preflight=lambda: True, root=work,
    )
    events = [e["message"] for e in ledger.recent_events(result.run_id, limit=200)]
    status = ledger.story_rows(result.run_id)[0]["status"]
    return status, seen, gate_calls, events


# ---------------------------------------------------------------------------
# AC1 — no-commit round + the re-polled gate is green → the merge is retried
# ---------------------------------------------------------------------------

def test_no_commit_round_with_green_regate_retries_the_merge(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    head = _git(work, "rev-parse", "HEAD")

    status, seen, gate_calls, events = _run(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED, build_mod.CR_SUCCESS]
    )

    assert status == "DONE", "\n".join(events)
    assert len(seen["bugfix"]) == 1  # the green re-poll did not burn another round
    assert len(seen["merge"]) == 1  # the retried merge dispatched the merge agent
    assert gate_calls[:2] == [build_mod.CR_FAILED, build_mod.CR_SUCCESS]
    assert _git(work, "rev-parse", "HEAD") == head  # nothing committed or pushed
    assert any("re-ran CI" in m and "retrying the merge" in m for m in events)


# ---------------------------------------------------------------------------
# AC2 — only the high-risk approval gate is still red → AWAITING_APPROVAL
# ---------------------------------------------------------------------------

def test_only_the_approval_gate_red_after_the_rerun_parks_awaiting_approval(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    before = _view(
        ("smoke (macos)", build_mod.CR_FAILED), (GATE_CHECK, build_mod.CR_FAILED),
        ("tests", build_mod.CR_SUCCESS),
    )
    after = _view(
        ("smoke (macos)", build_mod.CR_SUCCESS), (GATE_CHECK, build_mod.CR_FAILED),
        ("tests", build_mod.CR_SUCCESS),
    )

    status, seen, _, events = _run(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED] * 5, views=[before, after]
    )

    assert status == "AWAITING_APPROVAL", "\n".join(events)
    assert len(seen["bugfix"]) == 1  # no further rounds burnt on a human gate
    assert seen["merge"] == []


# ---------------------------------------------------------------------------
# AC3 — still red on a non-gate check → the next round names it; then exhausted
# ---------------------------------------------------------------------------

def test_still_red_check_is_named_to_the_next_round_then_exhausts(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    red = _view(
        ("smoke (macos)", build_mod.CR_FAILED), ("tests", build_mod.CR_SUCCESS),
        high_risk=False,
    )

    status, seen, _, events = _run(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED] * 5, views=[red]
    )

    assert len(seen["bugfix"]) == build_mod.MAX_BUGFIX_ATTEMPTS
    assert "smoke (macos)" in seen["bugfix"][1]
    assert "smoke (macos)" not in seen["bugfix"][0]
    assert status == "FAILED", "\n".join(events)  # no commit anywhere: existing path


# ---------------------------------------------------------------------------
# AC4 — the re-poll's cap lapses with checks still running → awaiting CI
# ---------------------------------------------------------------------------

def test_regate_cap_lapsing_reports_awaiting_ci_never_done(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)

    status, seen, _, events = _run(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED, build_mod.CR_PENDING]
    )

    assert status == "NEEDS_ATTENTION", "\n".join(events)
    assert len(seen["bugfix"]) == 1
    assert seen["merge"] == []
    ledger = Ledger(tmp_path / "loop.db")
    notes = ledger.story_park_notes(ledger.latest_run_id())
    assert "awaiting CI" in notes[SID]
    assert "PR #100" in notes[SID]


# ---------------------------------------------------------------------------
# AC5 — a round that committed a fix is unchanged (35.5-001 adjudicates it)
# ---------------------------------------------------------------------------

def test_committing_round_is_adjudicated_by_35_5_001_not_regated(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)

    status, _, gate_calls, events = _run(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED] * 10, commit=True
    )

    # 1 merge gate + one adjudication poll per pushed round — no extra re-poll.
    assert len(gate_calls) == 1 + build_mod.MAX_BUGFIX_ATTEMPTS
    assert status == "NEEDS_ATTENTION", "\n".join(events)  # fix on origin: parked
    assert not any("re-ran CI" in m for m in events)


def test_ci_rerun_park_note_label_wording() -> None:
    f = build_mod._park_note_label
    assert f("abcdef1234", 7, "awaiting CI", lead="CI re-run") == (
        "CI re-run · awaiting CI · abcdef1 · PR #7"
    )


def test_local_branch_sha_reads_the_head_and_is_none_when_unreadable(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    assert build_mod._local_branch_sha(work, BRANCH) == _git(work, "rev-parse", "HEAD")
    assert build_mod._local_branch_sha(work, "feature/missing") is None

    def boom(*_a, **_k):
        raise OSError("git unavailable")

    monkeypatch.setattr(build_mod, "_git", boom)
    assert build_mod._local_branch_sha(work, BRANCH) is None


def _regate(tmp_path, monkeypatch, gate):
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", lambda *_a, **_k: gate)
    return build_mod._regate_after_ci_rerun(
        _story(), None, "run", 100, None, None, tmp_path
    )


def test_regate_without_a_gate_result_defers_to_the_exhausted_path(tmp_path, monkeypatch) -> None:
    assert _regate(tmp_path, monkeypatch, None) == build_mod._CIRerunRegate()


def test_regate_with_an_unclassified_status_defers_to_the_exhausted_path(
    tmp_path, monkeypatch
) -> None:
    gate = _MergeCIGate(_GATE_BLOCK, "unclassified", "no verdict", 1, 0.0)
    monkeypatch.setattr(build_mod, "_merge_gate_only_block", lambda *_a, **_k: False)
    assert _regate(tmp_path, monkeypatch, gate) == build_mod._CIRerunRegate()
