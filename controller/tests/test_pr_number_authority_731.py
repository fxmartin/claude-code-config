# ABOUTME: Issue #731 — the controller's change-request number is authoritative.
# ABOUTME: An agent's self-reported pr_number never overwrites one the controller already holds.

from __future__ import annotations

from sdlc.build import Ledger, _adopt_pr, _extract_pr
from sdlc.dispatch import AgentResult


def _result(**data) -> AgentResult:
    return AgentResult(agent_type="review", data=data, raw="")


def test_controller_number_wins_over_agent_report() -> None:
    """The 2026-09-27 incident: the controller opened #730, the reviewer reported
    the *issue* number 728, and the run's PR became 728."""
    assert _extract_pr(_result(pr_number=728), 730) == 730


def test_agent_number_is_adopted_when_controller_has_none() -> None:
    assert _extract_pr(_result(pr_number=728), None) == 728


def test_non_integer_report_is_ignored() -> None:
    assert _extract_pr(_result(pr_number="730"), None) is None
    assert _extract_pr(_result(), 730) == 730


def _events(ledger: Ledger, run_id: str) -> list[dict]:
    return ledger.recent_events(run_id, limit=50)


def test_mismatch_is_logged_and_controller_number_kept(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("issue-728", "fix")
    kept = _adopt_pr(ledger, run_id, "issue-728", _result(pr_number=728), 730)
    assert kept == 730
    warns = [e for e in _events(ledger, run_id) if e["level"] == "warn"]
    assert any("#728" in e["message"] and "#730" in e["message"] for e in warns)


def test_matching_report_logs_nothing(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("issue-728", "fix")
    assert _adopt_pr(ledger, run_id, "issue-728", _result(pr_number=730), 730) == 730
    assert not [e for e in _events(ledger, run_id) if e["level"] == "warn"]
