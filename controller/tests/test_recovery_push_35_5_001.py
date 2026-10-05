# ABOUTME: Tests for story 35.5-001 — committed recovery work is pushed before a story is failed.
# ABOUTME: Real temp git repos + a bare origin; the CI gate seam is faked, never the network.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sdlc import build as build_mod
from sdlc.build import (
    _GATE_BLOCK,
    _GATE_PASS,
    BuildOptions,
    Ledger,
    _exhausted_status,
    _MergeCIGate,
    _run_bugfix,
    _run_bugfix_round,
    _teardown_story_workdir,
    _unpushed_commits,
    status_snapshot,
)
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult

SID = "35.5-001"
BRANCH = f"feature/{SID}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, *, ci: bool = True) -> Path:
    """A clone with a bare ``origin``, ``feature/<id>`` pushed once and tracking."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README").write_text("base\n")
    if ci:
        (work / ".gitlab-ci.yml").write_text("test:\n  script: [true]\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: base")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", BRANCH)
    _git(work, "push", "-u", "origin", BRANCH)
    return work


def _commit(work: Path, name: str = "fix.txt") -> str:
    (work / name).write_text(name)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"fix: {name}")
    return _git(work, "rev-parse", "HEAD")


def _remote_sha(work: Path) -> str:
    return _git(work, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()[0]


def _story() -> Story:
    return Story(SID, "Recovery push", "35", "fleet-execution", "epic-35.md", "Must", 5, "py", [])


def _ledger(tmp_path: Path) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-35", "sequential")
    ledger.story_upsert(run_id, SID, "35", "Recovery push", "Must", 5, "py", "", None, "IN_PROGRESS")
    return ledger, run_id


def _messages(ledger: Ledger, run_id: str) -> list[str]:
    return [e["message"] for e in ledger.recent_events(run_id, limit=100)]


def _envelope(**over) -> dict:
    data = {
        "failure_category": "TEST_FAILURE",
        "root_cause": "x",
        "fix_status": "FIXED",
        "tests_passing": False,
        "bugs_fixed": 1,
        "tests_fixed": 1,
    }
    data.update(over)
    return data


def _dispatch(data: dict, *, commit_in: Path | None = None):
    """A bugfix dispatcher; optionally commits a fix in ``commit_in`` first."""

    def run(agent_type, prompt, story=None, **kwargs):
        if commit_in is not None:
            _commit(commit_in)
        return AgentResult(agent_type=agent_type, data=data, raw="")

    return run


def _gate(status: str, verdict: str | None = None):
    verdict = verdict or (_GATE_PASS if status == build_mod.CR_SUCCESS else _GATE_BLOCK)
    calls: list[tuple] = []

    def fake(stage, ledger, run_id, story, pr_number, opts, **kw):
        calls.append((stage, pr_number, kw.get("repo_root")))
        return _MergeCIGate(
            verdict=verdict, status=status, reason=f"pipeline {status}",
            polls=1, waited_s=0.0, ci_configured=True,
        )

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _round(work, ledger, run_id, data, *, pr=7, commit=True):
    return _run_bugfix_round(
        _story(), "review", "failure", BuildOptions(), ledger, run_id,
        _dispatch(data, commit_in=work if commit else None),
        root=work, pr_number=pr,
    )


# ---------------------------------------------------------------------------
# _unpushed_commits
# ---------------------------------------------------------------------------

def test_unpushed_commits_counts_and_names_the_tip(tmp_path) -> None:
    work = _repo(tmp_path)
    assert _unpushed_commits(work, BRANCH) == (0, "")
    sha = _commit(work)
    assert _unpushed_commits(work, BRANCH) == (1, sha)


def test_unpushed_commits_degrades_to_none_without_a_branch(tmp_path) -> None:
    work = _repo(tmp_path)
    assert _unpushed_commits(work, "feature/nope") == (0, "")
    assert _unpushed_commits(tmp_path / "missing", BRANCH) == (0, "")


def test_unpushed_commits_on_never_pushed_branch_counts_from_base(tmp_path) -> None:
    work = _repo(tmp_path)
    _git(work, "checkout", "-b", "feature/fresh", "main")
    sha = _commit(work)
    assert _unpushed_commits(work, "feature/fresh") == (1, sha)


# ---------------------------------------------------------------------------
# AC1 — FIXED + not green + ahead: push, then CI adjudicates
# ---------------------------------------------------------------------------

def test_fixed_not_green_ahead_pushes_then_green_ci_counts_as_passing(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_SUCCESS)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)

    out = _round(work, ledger, run_id, _envelope())

    sha = _git(work, "rev-parse", "HEAD")
    assert out.fixed is True
    assert out.pushed_sha == sha
    assert _remote_sha(work) == sha
    assert f"bugfix commit {sha} pushed to {BRANCH}; CI adjudicates" in _messages(ledger, run_id)
    assert gate.calls == [("merge", 7, work)]  # the #793 gate seam, same grace/cap


def test_red_ci_is_reported_for_one_more_round(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", _gate(build_mod.CR_FAILED))

    out = _round(work, ledger, run_id, _envelope())

    assert out.fixed is False and out.ci_red is True
    assert out.pushed_sha == _git(work, "rev-parse", "HEAD")


def test_pending_timeout_is_awaiting_not_red(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", _gate(build_mod.CR_PENDING))

    out = _round(work, ledger, run_id, _envelope())

    assert out.fixed is False and out.ci_red is False
    assert out.pushed_sha


def test_no_ci_config_keeps_the_local_verdict_but_still_pushes(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path, ci=False)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_SUCCESS)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)

    out = _round(work, ledger, run_id, _envelope())

    assert out.fixed is False
    assert out.pushed_sha == _remote_sha(work)
    assert gate.calls == []  # no CI to adjudicate


def test_no_pr_yet_keeps_the_local_verdict_but_still_pushes(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_SUCCESS)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)

    out = _round(work, ledger, run_id, _envelope(), pr=None)

    assert out.fixed is False and out.pushed_sha == _remote_sha(work)
    assert gate.calls == []


def test_ci_none_status_is_todays_verdict(tmp_path, monkeypatch) -> None:
    """A gate that finds no pipeline is not a green: the local verdict stands."""
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    monkeypatch.setattr(
        build_mod, "_run_merge_ci_gate", _gate(build_mod.CR_NONE, verdict=_GATE_PASS)
    )
    out = _round(work, ledger, run_id, _envelope())
    assert out.fixed is False and out.ci_red is False


def test_not_ahead_never_pushes_or_polls(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_SUCCESS)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)

    out = _round(work, ledger, run_id, _envelope(), commit=False)

    assert out.fixed is False and out.pushed_sha is None
    assert gate.calls == []


def test_green_local_verdict_does_not_wait_on_ci(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_FAILED)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)

    out = _round(work, ledger, run_id, _envelope(tests_passing=True))

    assert out.fixed is True and gate.calls == []


def test_unfixed_round_is_never_pushed(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", _gate(build_mod.CR_SUCCESS))
    before = _remote_sha(work)

    out = _round(work, ledger, run_id, _envelope(fix_status="UNFIXED"))

    assert out.fixed is False and out.pushed_sha is None
    assert _remote_sha(work) == before


def test_push_is_a_lease_on_the_pre_round_sha(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", _gate(build_mod.CR_SUCCESS))
    leases: list[str | None] = []
    real = build_mod._push_story_branch

    def spy(root, branch, *, lease_sha=None):
        leases.append(lease_sha)
        return real(root, branch, lease_sha=lease_sha)

    monkeypatch.setattr(build_mod, "_push_story_branch", spy)
    pre = _remote_sha(work)

    _round(work, ledger, run_id, _envelope())

    assert leases == [pre]


def test_rejected_push_is_not_adjudicated(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    gate = _gate(build_mod.CR_SUCCESS)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)
    # Someone else advances the remote branch mid-round → the lease must refuse.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(tmp_path / "origin.git"), str(other)],
                   check=True, capture_output=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    _git(other, "checkout", BRANCH)
    _commit(other, "theirs.txt")
    _git(other, "push", "origin", BRANCH)

    out = _round(work, ledger, run_id, _envelope())

    assert out.fixed is False and out.pushed_sha is None
    assert gate.calls == []
    assert any("could not be pushed" in m for m in _messages(ledger, run_id))


# ---------------------------------------------------------------------------
# AC4 — baseline_failures are excluded from the tests_passing verdict
# ---------------------------------------------------------------------------

def test_failures_all_in_baseline_count_as_passing(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    data = _envelope(
        failing_tests=["bats: sync_spec"], baseline_failures=["bats: sync_spec"]
    )
    fixed = _run_bugfix(
        _story(), "review", "f", BuildOptions(), ledger, run_id, _dispatch(data)
    )
    assert fixed is True
    assert any("bats: sync_spec" in m and "baseline" in m for m in _messages(ledger, run_id))


def test_a_new_failure_beside_a_baseline_one_still_fails(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    data = _envelope(
        failing_tests=["bats: sync_spec", "test_new"], baseline_failures=["bats: sync_spec"]
    )
    assert _run_bugfix(
        _story(), "review", "f", BuildOptions(), ledger, run_id, _dispatch(data)
    ) is False


def test_baseline_without_a_failing_list_changes_nothing(tmp_path) -> None:
    """An older agent / an agent that did not enumerate failures: no new verdict."""
    ledger, run_id = _ledger(tmp_path)
    data = _envelope(baseline_failures=["bats: sync_spec"])
    assert _run_bugfix(
        _story(), "review", "f", BuildOptions(), ledger, run_id, _dispatch(data)
    ) is False


def test_baseline_never_rescues_an_unfixed_round(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    data = _envelope(fix_status="UNFIXED", failing_tests=["a"], baseline_failures=["a"])
    assert _run_bugfix(
        _story(), "review", "f", BuildOptions(), ledger, run_id, _dispatch(data)
    ) is False


def test_schema_accepts_baseline_failures_and_omission() -> None:
    from sdlc.contracts import validate_response

    validate_response("bugfix", _envelope(baseline_failures=["a"], failing_tests=["a", "b"]))
    validate_response("bugfix", _envelope())


def test_schema_rejects_non_string_baseline_entries() -> None:
    from sdlc.contracts import ContractError, validate_response

    with pytest.raises(ContractError):
        validate_response("bugfix", _envelope(baseline_failures=[1]))


# ---------------------------------------------------------------------------
# AC2 — exhausted recovery pushes committed work and parks, for every kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["reported", "dispatch", "ci-gate", "contract", "merge-unverified"])
def test_exhausted_with_commits_ahead_pushes_and_parks(tmp_path, kind) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    sha = _commit(work)

    status = _exhausted_status(kind, "build", SID, 7, ledger, run_id, workdir=work)

    assert status == "NEEDS_ATTENTION"
    assert _remote_sha(work) == sha
    assert (
        f"recovery exhausted; 1 unpushed commit(s) pushed to {BRANCH} ({sha})"
        in _messages(ledger, run_id)
    )


def test_exhausted_with_no_commit_is_failed(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    assert _exhausted_status("reported", "build", SID, 7, ledger, run_id, workdir=work) == "FAILED"


def test_exhausted_after_an_already_pushed_fix_parks(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    sha = _commit(work)
    _git(work, "push", "origin", BRANCH)

    status = _exhausted_status(
        "reported", "review", SID, 7, ledger, run_id, workdir=work,
        pushed_fix=build_mod._PushedFix(sha=sha, ci_red=True),
    )

    assert status == "NEEDS_ATTENTION"


def test_exhausted_push_failure_still_parks_and_says_so(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    _commit(work)
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    status = _exhausted_status("reported", "build", SID, None, ledger, run_id, workdir=work)

    assert status == "NEEDS_ATTENTION"
    assert any(
        e["level"] == "warn" and "could not be pushed" in e["message"]
        for e in ledger.recent_events(run_id, limit=50)
    )


def test_exhausted_without_workdir_keeps_todays_behaviour(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # not a repo
    ledger, run_id = _ledger(tmp_path)
    assert _exhausted_status("reported", "build", SID, None, ledger, run_id) == "FAILED"


# ---------------------------------------------------------------------------
# AC3 — teardown: preserved means on the remote
# ---------------------------------------------------------------------------

def _teardown_setup(tmp_path, monkeypatch, status="FAILED"):
    work = _repo(tmp_path)
    monkeypatch.chdir(work)
    ledger, run_id = _ledger(tmp_path)
    wt = tmp_path / "wt"
    _git(work, "checkout", "main")
    _git(work, "worktree", "add", str(wt), BRANCH)
    ledger.set_story_worktree(run_id, SID, str(wt))
    ledger.set_story_status(run_id, SID, status)
    return work, wt, ledger, run_id


def test_teardown_pushes_unpushed_commits_then_removes(tmp_path, monkeypatch) -> None:
    work, wt, ledger, run_id = _teardown_setup(tmp_path, monkeypatch)
    sha = _commit(wt)

    _teardown_story_workdir(ledger, run_id, SID, real_run=True)

    assert not wt.exists()
    assert _remote_sha(work) == sha


def test_teardown_keeps_worktree_and_logs_sha_on_rejected_push(tmp_path, monkeypatch) -> None:
    work, wt, ledger, run_id = _teardown_setup(tmp_path, monkeypatch)
    sha = _commit(wt)
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    _teardown_story_workdir(ledger, run_id, SID, real_run=True)

    assert wt.exists()
    assert any(sha in m and "worktree kept" in m for m in _messages(ledger, run_id))


def test_teardown_never_resurrects_a_merged_story_branch(tmp_path, monkeypatch) -> None:
    work, wt, ledger, run_id = _teardown_setup(tmp_path, monkeypatch, status="DONE")
    _commit(wt)
    before = _remote_sha(work)

    _teardown_story_workdir(ledger, run_id, SID, real_run=True)

    assert not wt.exists()
    assert _remote_sha(work) == before


def test_teardown_with_nothing_to_push_just_removes(tmp_path, monkeypatch) -> None:
    _work, wt, ledger, run_id = _teardown_setup(tmp_path, monkeypatch)
    _teardown_story_workdir(ledger, run_id, SID, real_run=True)
    assert not wt.exists()


# ---------------------------------------------------------------------------
# AC5 — status wording (`fix pushed · awaiting CI` / `· CI red`)
# ---------------------------------------------------------------------------

def test_park_note_label_wording() -> None:
    f = build_mod._park_note_label
    assert f("abcdef1234", 7, "awaiting CI") == "fix pushed · awaiting CI · abcdef1 · PR #7"
    assert f("abcdef1234", 7, "CI red") == "fix pushed · CI red · abcdef1 · PR #7"
    assert f("abcdef1234", None, None) == "fix pushed · abcdef1"


def test_exhausted_park_records_a_status_detail_in_the_snapshot(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    sha = _commit(work)

    _exhausted_status("reported", "build", SID, 7, ledger, run_id, workdir=work)
    ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")

    story = status_snapshot(ledger, run_id)["stories"][0]
    assert story["status_detail"].startswith("fix pushed")
    assert sha[:7] in story["status_detail"] and "PR #7" in story["status_detail"]


def test_ci_red_park_reads_ci_red(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    sha = _commit(work)
    _git(work, "push", "origin", BRANCH)

    _exhausted_status(
        "reported", "review", SID, 7, ledger, run_id, workdir=work,
        pushed_fix=build_mod._PushedFix(sha=sha, ci_red=True),
    )
    ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")

    detail = status_snapshot(ledger, run_id)["stories"][0]["status_detail"]
    assert "CI red" in detail


def test_status_detail_only_shows_while_the_story_is_parked(tmp_path) -> None:
    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    _commit(work)
    _exhausted_status("reported", "build", SID, 7, ledger, run_id, workdir=work)
    ledger.set_story_status(run_id, SID, "DONE")

    assert status_snapshot(ledger, run_id)["stories"][0]["status_detail"] is None


def test_cli_status_prints_the_detail(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    work = _repo(tmp_path)
    ledger, run_id = _ledger(tmp_path)
    _commit(work)
    _exhausted_status("reported", "build", SID, 7, ledger, run_id, workdir=work)
    ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")

    result = CliRunner().invoke(app, ["status", "--db", str(ledger.db_path), "--run", run_id])

    assert result.exit_code == 0
    assert "fix pushed" in result.output


def test_dashboard_renders_status_detail_and_tracks_it_for_the_fleet_view() -> None:
    from sdlc import dashboard

    html = dashboard.__dict__.get("INDEX_HTML") or dashboard.__dict__.get("_PAGE") or ""
    assert "status_detail" in html or "status_detail" in Path(dashboard.__file__).read_text()
    # the SSE change token for a relayed worker run moves when the detail does
    snap = {"stories": [{"story_id": "x", "status": "NEEDS_ATTENTION", "status_detail": "a"}]}
    other = {"stories": [{"story_id": "x", "status": "NEEDS_ATTENTION", "status_detail": "b"}]}

    class Relay:
        def __init__(self, s):
            self.s = s

        def get(self, origin, run_id):
            return self.s

    assert dashboard._remote_detail_token(Relay(snap), "o", "r") != dashboard._remote_detail_token(
        Relay(other), "o", "r"
    )


# ---------------------------------------------------------------------------
# Loop: red CI buys one more bugfix round; green resolves; local parity
# ---------------------------------------------------------------------------

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
        "merged_at": "2026-06-12T00:00:00Z",
    },
}


def _run_loop(work, tmp_path, monkeypatch, gate_statuses, bugfix_green=False):
    """Drive run_build with a review that rejects once and a committing bugfix agent."""
    monkeypatch.chdir(work)
    statuses = list(gate_statuses)
    gate_calls: list[str] = []

    def gate(stage, ledger, run_id, story, pr, opts, **kw):
        if stage != "merge":  # the real gate is a no-op outside the merge stage
            return None
        status = statuses.pop(0) if statuses else build_mod.CR_SUCCESS
        gate_calls.append(status)
        return _gate(status)(stage, ledger, run_id, story, pr, opts)

    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", gate)
    seen = {"review": 0, "bugfix": 0}

    def dispatch(agent_type, prompt, story=None, **kw):
        if agent_type == "review":
            seen["review"] += 1
            if seen["review"] == 1:
                return AgentResult(agent_type, {
                    "pr_number": 100, "approval_status": "CHANGES_NEEDED",
                    "change_count": 1, "final_status": "REJECTED",
                }, "")
        if agent_type == "bugfix":
            seen["bugfix"] += 1
            _commit(work, f"fix{seen['bugfix']}.txt")
            return AgentResult(agent_type, _envelope(tests_passing=bugfix_green), "")
        return AgentResult(agent_type, _PAYLOADS[agent_type], "")

    result = build_mod.run_build(
        BuildOptions(scope="epic-35", skip_preflight=True, sequential=True, auto=True),
        queue=[_story()], ledger=Ledger(tmp_path / "loop.db"),
        dispatcher=dispatch, preflight=lambda: True, root=work,
    )
    return result, seen, gate_calls


def test_stage_loop_red_ci_runs_another_round_then_green_resolves(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    result, seen, gate_calls = _run_loop(
        work, tmp_path, monkeypatch, [build_mod.CR_FAILED, build_mod.CR_SUCCESS]
    )
    ev = [e["message"] for e in Ledger(tmp_path / "loop.db").recent_events(result.run_id, limit=60)]
    assert result.completed == 1, "\n".join(ev)
    assert seen["bugfix"] == 2  # red CI → one more round, not an immediate FAILED
    assert _remote_sha(work) == _git(work, "rev-parse", "HEAD")


def test_stage_loop_exhausted_on_red_ci_parks_with_the_fix_on_origin(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    result, seen, _ = _run_loop(work, tmp_path, monkeypatch, [build_mod.CR_FAILED] * 10)
    assert result.completed == 0
    assert result.failed == 0 and result.needs_attention == 1  # parked, not FAILED
    assert _remote_sha(work) == _git(work, "rev-parse", "HEAD")
    rows = Ledger(tmp_path / "loop.db").story_rows(Ledger(tmp_path / "loop.db").latest_run_id())
    assert rows[0]["status"] == "NEEDS_ATTENTION"


def test_stage_loop_without_ci_keeps_todays_verdict_but_parks(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path, ci=False)
    result, seen, gate_calls = _run_loop(work, tmp_path, monkeypatch, [])
    assert seen["bugfix"] == 1
    assert result.failed == 0 and result.needs_attention == 1  # fix on origin: parked
    assert _remote_sha(work) == _git(work, "rev-parse", "HEAD")


def test_remote_tracking_sha_names_the_tracked_tip_or_none(tmp_path) -> None:
    work = _repo(tmp_path)
    assert build_mod._remote_tracking_sha(work, BRANCH) == _remote_sha(work)
    assert build_mod._remote_tracking_sha(work, "feature/nope") is None


def test_try_push_branch_reports_success_and_failure(tmp_path) -> None:
    work = _repo(tmp_path)
    _commit(work)
    ok, _ = build_mod._try_push_branch(work, BRANCH)
    assert ok
    assert _remote_sha(work) == _git(work, "rev-parse", "HEAD")
    ok, err = build_mod._try_push_branch(work, "feature/nope")
    assert not ok and err


def test_try_push_branch_swallows_os_errors(tmp_path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("git vanished")

    monkeypatch.setattr(build_mod, "_push_story_branch", boom)
    assert build_mod._try_push_branch(tmp_path, BRANCH) == (False, "git vanished")


def test_story_park_notes_without_a_ledger_is_empty(tmp_path) -> None:
    assert Ledger(tmp_path / "missing.db").story_park_notes("run-x") == {}
