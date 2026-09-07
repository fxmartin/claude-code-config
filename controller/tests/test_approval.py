# ABOUTME: Tests for the read-only change-request approval probe (Story 32.2-002).
# ABOUTME: Label / approving review / merged / closed fixtures on GitHub and GitLab.

from __future__ import annotations

import json

import pytest

from sdlc.approval import poll_approval
from sdlc.issue_host import IssueHostError, RunResult


class FakeRunner:
    """Records every host-CLI argv and replays a canned response per verb."""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout: float = 30.0) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        for key, payload in self._responses.items():
            if key in " ".join(argv):
                if isinstance(payload, Exception):
                    raise payload
                return RunResult(returncode=0, stdout=json.dumps(payload), stderr="")
        return RunResult(returncode=1, stdout="", stderr=f"no fixture for {argv}")


def _github(monkeypatch, runner) -> None:
    monkeypatch.setattr("sdlc.approval.resolve_host", lambda _root: "github")
    monkeypatch.setattr("sdlc.approval.repo_runner", lambda _root: runner)


def _gitlab(monkeypatch, runner) -> None:
    monkeypatch.setattr("sdlc.approval.resolve_host", lambda _root: "gitlab")
    monkeypatch.setattr("sdlc.approval.repo_runner", lambda _root: runner)


# --- GitHub fixtures -------------------------------------------------------


def test_the_risk_approved_label_reads_as_approved(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "pr view": {
            "state": "OPEN",
            "labels": [{"name": "risk:high"}, {"name": "risk-approved"}],
            "reviewDecision": "REVIEW_REQUIRED",
            "reviews": [],
        }
    })
    _github(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict is not None
    assert verdict.state == "open"
    assert verdict.approved is True
    assert verdict.signal == "risk-approved label"


def test_an_approving_review_reads_as_approved(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "pr view": {
            "state": "OPEN",
            "labels": [{"name": "risk:high"}],
            "reviewDecision": "APPROVED",
            "reviews": [{"state": "APPROVED"}],
        }
    })
    _github(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict.approved is True
    assert verdict.signal == "approving review"


def test_a_stale_approving_review_still_counts(tmp_path, monkeypatch) -> None:
    """`reviewDecision` can be CHANGES_REQUESTED while an APPROVED review exists."""
    runner = FakeRunner({
        "pr view": {
            "state": "OPEN",
            "labels": [],
            "reviewDecision": "REVIEW_REQUIRED",
            "reviews": [{"state": "COMMENTED"}, {"state": "APPROVED"}],
        }
    })
    _github(monkeypatch, runner)

    assert poll_approval(tmp_path, 12).approved is True


def test_an_unapproved_pr_reads_as_open_and_unapproved(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "pr view": {
            "state": "OPEN",
            "labels": [{"name": "risk:high"}],
            "reviewDecision": "REVIEW_REQUIRED",
            "reviews": [{"state": "COMMENTED"}],
        }
    })
    _github(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict.state == "open"
    assert verdict.approved is False
    assert verdict.signal == ""


def test_a_hand_merged_pr_reads_as_merged(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "pr view": {"state": "MERGED", "labels": [], "reviewDecision": None, "reviews": []}
    })
    _github(monkeypatch, runner)

    assert poll_approval(tmp_path, 12).state == "merged"


def test_a_closed_pr_reads_as_closed(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "pr view": {"state": "CLOSED", "labels": [], "reviewDecision": None, "reviews": []}
    })
    _github(monkeypatch, runner)

    assert poll_approval(tmp_path, 12).state == "closed"


# --- AC4: read-only ---------------------------------------------------------


def test_polling_only_ever_reads(tmp_path, monkeypatch) -> None:
    """No merge/edit/close verb may appear in anything the probe runs (AC4)."""
    runner = FakeRunner({
        "pr view": {"state": "OPEN", "labels": [], "reviewDecision": None, "reviews": []}
    })
    _github(monkeypatch, runner)

    poll_approval(tmp_path, 12)

    assert runner.calls == [
        ["gh", "pr", "view", "12", "--json", "state,labels,reviewDecision,reviews"]
    ]
    mutating = {"merge", "edit", "close", "comment", "create", "review", "ready"}
    for argv in runner.calls:
        assert not mutating & set(argv)


# --- best-effort degradation ------------------------------------------------


def test_a_host_failure_yields_none_rather_than_raising(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({"pr view": IssueHostError("gh not found on PATH")})
    _github(monkeypatch, runner)

    assert poll_approval(tmp_path, 12) is None


def test_an_undetectable_host_yields_none(tmp_path, monkeypatch) -> None:
    def boom(_root):
        raise IssueHostError("could not determine code host from git remote")

    monkeypatch.setattr("sdlc.approval.resolve_host", boom)

    assert poll_approval(tmp_path, 12) is None


def test_unparseable_output_yields_none(tmp_path, monkeypatch) -> None:
    """An empty/garbage payload must not be read as "closed" and fail the job."""

    class Empty:
        calls: list[list[str]] = []

        def __call__(self, argv, timeout: float = 30.0) -> RunResult:
            return RunResult(returncode=0, stdout="", stderr="")

    _github(monkeypatch, Empty())

    assert poll_approval(tmp_path, 12) is None


# --- GitLab -----------------------------------------------------------------


def test_a_gitlab_mr_reads_labels_state_and_approvals(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "mr view": {
            "iid": 12,
            "state": "opened",
            "labels": ["risk:high", "risk-approved"],
        },
    })
    _gitlab(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict.state == "open"
    assert verdict.approved is True
    assert verdict.signal == "risk-approved label"


def test_a_gitlab_mr_reads_an_approver_as_approved(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({
        "mr view": {"iid": 12, "state": "opened", "labels": []},
        "approvals": {"approved": True, "approved_by": [{"user": {"username": "fx"}}]},
    })
    _gitlab(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict.approved is True
    assert verdict.signal == "approving review"


def test_a_gitlab_merged_mr_reads_as_merged(tmp_path, monkeypatch) -> None:
    runner = FakeRunner({"mr view": {"iid": 12, "state": "merged", "labels": []}})
    _gitlab(monkeypatch, runner)

    assert poll_approval(tmp_path, 12).state == "merged"


def test_a_gitlab_approvals_failure_degrades_to_unapproved(tmp_path, monkeypatch) -> None:
    """The approvals API is premium-gated — its absence must not crash the poll."""
    runner = FakeRunner({
        "mr view": {"iid": 12, "state": "opened", "labels": []},
        "approvals": IssueHostError("403 forbidden"),
    })
    _gitlab(monkeypatch, runner)

    verdict = poll_approval(tmp_path, 12)

    assert verdict.state == "open"
    assert verdict.approved is False


def test_a_view_with_no_state_yields_none(tmp_path, monkeypatch) -> None:
    """An adapter that answered but named no state has not really read the CR."""
    from sdlc.issue_host import ChangeRequestApproval

    class Adapter:
        def cr_approval(self, _ref):
            return ChangeRequestApproval(state=None, labels=("risk-approved",))

    monkeypatch.setattr("sdlc.approval.resolve_host", lambda _root: "github")
    monkeypatch.setattr("sdlc.approval.get_adapter", lambda _h, runner=None: Adapter())

    assert poll_approval(tmp_path, 12) is None


def test_an_unexpected_adapter_crash_yields_none(tmp_path, monkeypatch) -> None:
    """A poll must never take a night's scheduler down (AC4: stops cleanly)."""

    class Adapter:
        def cr_approval(self, _ref):
            raise RuntimeError("something entirely unexpected")

    monkeypatch.setattr("sdlc.approval.resolve_host", lambda _root: "github")
    monkeypatch.setattr("sdlc.approval.get_adapter", lambda _h, runner=None: Adapter())

    assert poll_approval(tmp_path, 12) is None


def test_an_injected_runner_replaces_the_repo_scoped_one(tmp_path, monkeypatch) -> None:
    """An explicit runner wins, and no cwd-scoped one is built behind its back."""
    runner = FakeRunner({
        "pr view": {"state": "OPEN", "labels": [], "reviewDecision": None, "reviews": []}
    })
    monkeypatch.setattr("sdlc.approval.resolve_host", lambda _root: "github")

    def explode(_root):
        raise AssertionError("repo_runner must not be built when one is injected")

    monkeypatch.setattr("sdlc.approval.repo_runner", explode)

    verdict = poll_approval(tmp_path, 12, runner=runner)

    assert verdict.state == "open"
    assert runner.calls[0][:3] == ["gh", "pr", "view"]
