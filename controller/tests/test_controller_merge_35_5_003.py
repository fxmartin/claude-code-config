# ABOUTME: Story 35.5-003 — an approved, green, clean change request is merged by the
# ABOUTME: controller via cr_merge; a policy refusal parks instead of entering bugfix.
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from sdlc import build_issue
from sdlc import issue_host as ih
from sdlc.build import (
    BuildOptions,
    Ledger,
    _classify_merge_refusal,
    _merge_refusal_kind,
    _stage_failure_summary,
    render_merge_prompt,
    run_build,
)
from sdlc.cohort import Story
from test_build import FakeDispatcher, _default_payload
from test_change_request import FakeRunner

CLEAN = ih.ChangeRequestMergeability(clean=True, detail="CLEAN", labels=())
OP_COMMAND = "gh pr merge 100 --merge"


def _story() -> Story:
    return Story("g1-001", "Merge story", "35", "fleet-execution",
                 "epic-35.md", "Must", 5, "py", [])


class _Host:
    """Stubs the three build_issue seams the controller merge reads and writes."""

    def __init__(self, monkeypatch, *, mergeability=CLEAN, merge=None, merge_error=None):
        self.merges: list[object] = []
        self.mergeability = mergeability
        self.merge_error = merge_error
        self.merge = merge or ih.ChangeRequest(
            host=ih.GITHUB, ref="100", state="merged", merge_sha="abc1234"
        )
        monkeypatch.setattr(build_issue, "change_request_status",
                            lambda *a, **k: ih.CR_SUCCESS)
        monkeypatch.setattr(build_issue, "change_request_mergeability",
                            lambda *a, **k: self.mergeability)
        monkeypatch.setattr(build_issue, "change_request_merge_command",
                            lambda *a, **k: OP_COMMAND)
        monkeypatch.setattr(build_issue, "change_request_merge", self._do_merge)

    def _do_merge(self, ledger, story_id, cr_ref, *, runner=None):
        self.merges.append(cr_ref)
        if self.merge_error is not None:
            raise ih.IssueHostError(self.merge_error)
        return self.merge


def _run(tmp_path: Path, dispatcher: FakeDispatcher):
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-35", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[_story()], ledger=Ledger(db), dispatcher=dispatcher,
                       preflight=lambda: True)
    return result, db


def _events(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT message FROM events ORDER BY id")]
    finally:
        conn.close()


def _merge_sha(db: Path) -> str | None:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT merge_sha FROM stories").fetchone()[0]
    finally:
        conn.close()


def _agents(dispatcher: FakeDispatcher) -> list[str]:
    return [a for a, _ in dispatcher.calls]


def _blocked(reason: str = "MERGE_COMMAND_BLOCKED_PERMISSION", **extra) -> dict:
    return {"pr_number": 100, "merge_status": "FAILED", "merge_sha": "", "merged_at": "",
            "block_reason": reason, **extra}


# --- approved + green + clean -> cr_merge, no agent --------------------------


def test_clean_cr_is_merged_by_the_controller_with_no_merge_agent(tmp_path, monkeypatch):
    host = _Host(monkeypatch)
    dispatcher = FakeDispatcher()
    result, db = _run(tmp_path, dispatcher)

    assert result.completed == 1
    assert host.merges == [100]
    assert "merge" not in _agents(dispatcher)
    assert _merge_sha(db) == "abc1234"
    assert any("merged by the controller" in m for m in _events(db))


def test_merge_without_a_reported_sha_still_lands_and_says_so(tmp_path, monkeypatch):
    _Host(monkeypatch, merge=ih.ChangeRequest(host=ih.GITHUB, ref="100", state="merged"))
    dispatcher = FakeDispatcher()
    result, db = _run(tmp_path, dispatcher)

    assert result.completed == 1
    assert "merge" not in _agents(dispatcher)
    assert any("no landing sha" in m for m in _events(db))


# --- not cleanly mergeable -> the merge agent, as today ----------------------


@pytest.mark.parametrize("view", [
    ih.ChangeRequestMergeability(clean=False, detail="BEHIND"),
    ih.ChangeRequestMergeability(clean=False, detail="DIRTY"),
    ih.ChangeRequestMergeability(clean=True, detail="CLEAN", labels=("risk:high",)),
    None,
], ids=["behind", "conflicts", "high-risk-unapproved", "unreadable"])
def test_not_clean_cr_goes_to_the_merge_agent(tmp_path, monkeypatch, view):
    host = _Host(monkeypatch, mergeability=view)
    dispatcher = FakeDispatcher()
    result, _ = _run(tmp_path, dispatcher)

    assert host.merges == []
    assert _agents(dispatcher).count("merge") == 1
    assert result.completed == 1


def test_high_risk_with_approval_label_is_merged_by_the_controller(tmp_path, monkeypatch):
    host = _Host(monkeypatch, mergeability=ih.ChangeRequestMergeability(
        clean=True, detail="CLEAN", labels=("risk:high", "risk-approved")))
    dispatcher = FakeDispatcher()
    _run(tmp_path, dispatcher)

    assert host.merges == [100]
    assert "merge" not in _agents(dispatcher)


def test_red_ci_gate_never_reaches_the_controller_merge(tmp_path, monkeypatch):
    host = _Host(monkeypatch)
    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_FAILED)
    dispatcher = FakeDispatcher()
    _run(tmp_path, dispatcher)

    assert host.merges == []
    assert "merge" not in _agents(dispatcher)
    assert "bugfix" in _agents(dispatcher)


def test_non_policy_merge_error_falls_back_to_the_agent(tmp_path, monkeypatch):
    host = _Host(monkeypatch, merge_error="gh pr merge failed: HTTP 502 Bad Gateway")
    dispatcher = FakeDispatcher()
    result, db = _run(tmp_path, dispatcher)

    assert host.merges == [100]
    assert _agents(dispatcher).count("merge") == 1
    assert "bugfix" not in _agents(dispatcher)
    assert result.completed == 1
    assert any("dispatching the merge agent" in m for m in _events(db))


# --- policy refusal -> parked, no bugfix --------------------------------------


@pytest.mark.parametrize("refusal", [
    (
        "gh pr merge failed: Pull request is not mergeable: the base branch policy "
        "prohibits the merge. To use administrator privileges, add the `--admin` flag."
    ),
    "glab mr merge failed: 405 Branch protection prevents this merge",
])
def test_controller_merge_policy_refusal_parks_needs_attention(tmp_path, monkeypatch, refusal):
    host = _Host(monkeypatch, merge_error=refusal)
    dispatcher = FakeDispatcher()
    result, db = _run(tmp_path, dispatcher)

    assert result.story_status["g1-001"] == "NEEDS_ATTENTION"
    assert host.merges == [100]
    assert "merge" not in _agents(dispatcher)
    assert "bugfix" not in _agents(dispatcher)
    parked = [m for m in _events(db) if "merge blocked by policy" in m]
    assert len(parked) == 1
    assert refusal in parked[0] and OP_COMMAND in parked[0]


def test_missing_risk_approved_label_parks_awaiting_approval(tmp_path, monkeypatch):
    refusal = "gh pr merge failed: required label risk-approved is missing"
    _Host(monkeypatch, merge_error=refusal)
    dispatcher = FakeDispatcher()
    result, db = _run(tmp_path, dispatcher)

    assert result.story_status["g1-001"] == "AWAITING_APPROVAL"
    assert "bugfix" not in _agents(dispatcher)
    assert any(refusal in m and OP_COMMAND in m for m in _events(db))


def test_agent_reported_deny_floor_refusal_parks_without_bugfix(tmp_path, monkeypatch):
    """Run 46e76670: the agent's `--admin` was denied and reported blocked."""
    _Host(monkeypatch, mergeability=ih.ChangeRequestMergeability(clean=False, detail="BEHIND"))
    dispatcher = FakeDispatcher(overrides={
        ("merge", "g1-001"): _blocked(error_summary="gh pr merge 100 --merge --admin denied"),
    })
    result, db = _run(tmp_path, dispatcher)

    assert result.story_status["g1-001"] == "NEEDS_ATTENTION"
    assert _agents(dispatcher).count("merge") == 1
    assert "bugfix" not in _agents(dispatcher)
    assert any("merge blocked by policy" in m and OP_COMMAND in m for m in _events(db))


def test_policy_refusal_leaves_the_fix_budget_untouched(tmp_path, monkeypatch):
    """No bugfix stage row is ever written for a parked policy refusal."""
    _Host(monkeypatch, merge_error="base branch policy prohibits the merge")
    _, db = _run(tmp_path, FakeDispatcher())

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM stages WHERE stage_name = 'bugfix'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_ordinary_agent_failure_still_enters_the_bugfix_loop(tmp_path, monkeypatch):
    _Host(monkeypatch, mergeability=ih.ChangeRequestMergeability(clean=False, detail="DIRTY"))
    failing = iter([{"pr_number": 100, "merge_status": "FAILED", "merge_sha": "",
                     "merged_at": "", "error_summary": "rebase conflict"},
                    _default_payload("merge", _story())])
    dispatcher = FakeDispatcher(overrides={("merge", "g1-001"): lambda: next(failing)})
    result, _ = _run(tmp_path, dispatcher)

    assert "bugfix" in _agents(dispatcher)
    assert result.completed == 1


# --- classification -----------------------------------------------------------


def test_refusal_classification():
    assert _classify_merge_refusal("MERGE_COMMAND_BLOCKED_PERMISSION") == "policy_refused"
    assert _classify_merge_refusal("use the --admin flag") == "policy_refused"
    assert _classify_merge_refusal("Branch protection rule") == "policy_refused"
    assert _classify_merge_refusal("missing risk-approved label") == "awaiting_approval"
    assert _classify_merge_refusal("rebase conflict in a.py") is None


def test_refusal_kind_is_merge_only_and_summary_carries_the_text():
    data = _blocked(error_summary="denied by the deny floor: gh pr merge --admin")
    assert _merge_refusal_kind("merge", data) == "policy_refused"
    assert _merge_refusal_kind("build", data) is None
    assert "denied by the deny floor" in _stage_failure_summary("merge", data)


# --- the prompt ---------------------------------------------------------------


def test_merge_prompt_names_the_plain_command_and_forbids_admin():
    prompt = render_merge_prompt(_story(), 7)
    assert "plain `gh pr merge 7` command" in prompt
    assert "never `--admin`" in prompt
    assert '"MERGE_REFUSED_BY_POLICY"' in prompt


def test_merge_prompt_on_gitlab_names_glab():
    prompt = render_merge_prompt(_story(), 7, cr_terms=ih.GITLAB_CR_TERMS)
    assert "plain `glab mr merge 7` command" in prompt
    assert "never `--admin`" in prompt


# --- adapters -----------------------------------------------------------------


def _gh_view(**row) -> tuple[int, str, str]:
    return 0, json.dumps(row), ""


def test_github_mergeability_clean_and_behind():
    clean = FakeRunner({"pr view": _gh_view(
        mergeable="MERGEABLE", mergeStateStatus="CLEAN", labels=[{"name": "risk:high"}])})
    view = ih.GitHubAdapter(runner=clean).cr_mergeability("858")
    assert view.clean and view.labels == ("risk:high",)
    assert "mergeStateStatus" in " ".join(clean.calls[-1])

    behind = FakeRunner({"pr view": _gh_view(
        mergeable="MERGEABLE", mergeStateStatus="BEHIND", labels=[])})
    view = ih.GitHubAdapter(runner=behind).cr_mergeability("858")
    assert not view.clean and view.detail == "BEHIND"


def test_github_cr_merge_reports_the_landing_sha_and_the_operator_command():
    runner = FakeRunner({"mergeCommit": _gh_view(mergeCommit={"oid": "deadbeef"})})
    adapter = ih.GitHubAdapter(runner=runner)
    cr = adapter.cr_merge("858")
    assert cr.merge_sha == "deadbeef" and cr.state == "merged"
    assert runner.calls[0][:5] == ["gh", "pr", "merge", "858", "--merge"]
    assert "--admin" not in " ".join(runner.calls[0])
    assert adapter.merge_command("858") == "gh pr merge 858 --merge"


def test_github_cr_merge_survives_an_unreadable_landing_sha():
    runner = FakeRunner({"mergeCommit": (1, "", "boom"), "pr merge": (0, "", "")})
    assert ih.GitHubAdapter(runner=runner).cr_merge("858").merge_sha is None


def test_github_cr_merge_refusal_raises_with_the_host_text():
    runner = FakeRunner({"pr merge": (1, "", "base branch policy prohibits the merge")})
    with pytest.raises(ih.IssueHostError, match="base branch policy"):
        ih.GitHubAdapter(runner=runner).cr_merge("858")


def test_gitlab_mergeability_clean_conflicts_and_blocked():
    def view(**row):
        runner = FakeRunner({"mr view": (0, json.dumps(row), "")})
        return ih.GitLabAdapter(runner=runner).cr_mergeability("5")

    assert view(detailed_merge_status="mergeable", has_conflicts=False, labels=[]).clean
    assert not view(detailed_merge_status="mergeable", has_conflicts=True, labels=[]).clean
    blocked = view(detailed_merge_status="not_approved", labels=["risk:high"])
    assert not blocked.clean and blocked.detail == "not_approved"
    assert blocked.labels == ("risk:high",)


def test_gitlab_cr_merge_reports_the_landing_sha_and_the_operator_command():
    runner = FakeRunner({"mr view": (0, json.dumps({"merge_commit_sha": "c0ffee"}), "")})
    adapter = ih.GitLabAdapter(runner=runner)
    cr = adapter.cr_merge("5")
    assert cr.merge_sha == "c0ffee" and cr.state == "merged"
    assert runner.calls[0][:4] == ["glab", "mr", "merge", "5"]
    assert adapter.merge_command("5") == "glab mr merge 5 --yes"


def test_adapter_without_mergeability_raises_so_the_agent_handles_it():
    bare = SimpleNamespace(host="bare")
    with pytest.raises(ih.IssueHostError):
        ih.IssueHostAdapter.cr_mergeability(bare, "1")  # type: ignore[arg-type]
    with pytest.raises(ih.IssueHostError):
        ih.IssueHostAdapter.merge_command(bare, "1")  # type: ignore[arg-type]


# --- the build_issue seams ----------------------------------------------------


class _Adapter:
    cr_terms = ih.GITHUB_CR_TERMS

    def cr_mergeability(self, ref):
        return CLEAN

    def cr_merge(self, ref):
        return ih.ChangeRequest(host=ih.GITHUB, ref=ref, state="merged", merge_sha="s")

    def merge_command(self, ref):
        return f"gh pr merge {ref} --merge"


class _Exploding(_Adapter):
    def cr_mergeability(self, ref):
        raise ih.IssueHostError("down")

    def merge_command(self, ref):
        raise ih.IssueHostError("down")

    def cr_merge(self, ref):
        raise ih.IssueHostError("refused: branch protection")


def test_build_issue_seams_route_through_the_adapter(monkeypatch):
    monkeypatch.setattr(build_issue, "_cr_adapter", lambda *a, **k: _Adapter())
    assert build_issue.change_request_mergeability(None, "s", 7) == CLEAN
    assert build_issue.change_request_merge(None, "s", 7).merge_sha == "s"
    assert build_issue.change_request_merge_command(None, "s", 7) == "gh pr merge 7 --merge"


def test_build_issue_reads_are_best_effort_but_the_merge_propagates(monkeypatch):
    monkeypatch.setattr(build_issue, "_cr_adapter", lambda *a, **k: _Exploding())
    assert build_issue.change_request_mergeability(None, "s", 7) is None
    assert build_issue.change_request_merge_command(None, "s", 7) is None
    with pytest.raises(ih.IssueHostError, match="branch protection"):
        build_issue.change_request_merge(None, "s", 7)


def test_build_issue_seams_degrade_when_no_adapter_resolves(monkeypatch):
    monkeypatch.setattr(build_issue, "_cr_adapter", lambda *a, **k: None)
    assert build_issue.change_request_mergeability(None, "s", 7) is None
    assert build_issue.change_request_merge(None, "s", 7) is None
    assert build_issue.change_request_merge_command(None, "s", 7) is None
