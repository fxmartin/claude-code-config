# ABOUTME: Tests for the centralized degradation matrix and safe fallbacks (Story 20.5-002).
# ABOUTME: Covers each degradation path: parallel→serial, usage unavailable, rate-limit skipped, and recording.

from __future__ import annotations

import pytest

from sdlc.capability import MODE_PARALLEL, MODE_SERIAL
from sdlc.degradation import (
    Degradation,
    DegradationKind,
    DegradationPlan,
    evaluate_degradations,
)


# A harness that has every canonical capability (the built-in Claude shape) —
# requesting parallel on it must produce zero degradations.
_FULL = {
    "worktree_isolation": True,
    "parallel": True,
    "json_contract": True,
    "usage_tracking": True,
    "rate_limit_aware": True,
}

# A harness with no telemetry and no isolation (the codex shape).
_BARE = {
    "worktree_isolation": False,
    "parallel": False,
    "json_contract": True,
    "usage_tracking": False,
    "rate_limit_aware": False,
}


# ---------------------------------------------------------------------------
# AC1: a harness without worktree isolation degrades parallel → serial
# ---------------------------------------------------------------------------


def test_parallel_degrades_to_serial_without_worktree_isolation() -> None:
    plan = evaluate_degradations(
        "codex",
        {**_FULL, "worktree_isolation": False},
        requested_mode=MODE_PARALLEL,
    )
    assert plan.effective_mode == MODE_SERIAL
    assert plan.mode_degraded is True
    assert plan.has(DegradationKind.PARALLEL_TO_SERIAL)
    deg = next(d for d in plan.degradations if d.kind is DegradationKind.PARALLEL_TO_SERIAL)
    assert "worktree_isolation" in deg.missing
    assert "serial" in deg.message


def test_parallel_degrades_to_serial_without_parallel_capability() -> None:
    plan = evaluate_degradations(
        "codex",
        {**_FULL, "parallel": False},
        requested_mode=MODE_PARALLEL,
    )
    assert plan.effective_mode == MODE_SERIAL
    assert "parallel" in next(
        d for d in plan.degradations if d.kind is DegradationKind.PARALLEL_TO_SERIAL
    ).missing


def test_parallel_emits_exactly_one_mode_log_line() -> None:
    """AC1: the downgrade is announced by one explicit log line."""
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_PARALLEL)
    mode_lines = [
        line for line in plan.log_lines() if "mode=serial" in line
    ]
    assert len(mode_lines) == 1


def test_capable_harness_keeps_parallel_with_no_mode_degradation() -> None:
    plan = evaluate_degradations("claude", _FULL, requested_mode=MODE_PARALLEL)
    assert plan.effective_mode == MODE_PARALLEL
    assert plan.mode_degraded is False
    assert not plan.has(DegradationKind.PARALLEL_TO_SERIAL)


def test_serial_request_never_mode_degrades() -> None:
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_SERIAL)
    assert plan.effective_mode == MODE_SERIAL
    assert plan.mode_degraded is False
    assert not plan.has(DegradationKind.PARALLEL_TO_SERIAL)


# ---------------------------------------------------------------------------
# AC2: no usage / rate-limit semantics → usage "unavailable", backoff skipped
# ---------------------------------------------------------------------------


def test_missing_usage_tracking_records_usage_unavailable() -> None:
    plan = evaluate_degradations("codex", {**_FULL, "usage_tracking": False})
    assert plan.has(DegradationKind.USAGE_UNAVAILABLE)
    deg = next(d for d in plan.degradations if d.kind is DegradationKind.USAGE_UNAVAILABLE)
    assert "unavailable" in deg.message


def test_missing_rate_limit_awareness_skips_backoff() -> None:
    plan = evaluate_degradations("codex", {**_FULL, "rate_limit_aware": False})
    assert plan.has(DegradationKind.RATE_LIMIT_SKIPPED)
    deg = next(d for d in plan.degradations if d.kind is DegradationKind.RATE_LIMIT_SKIPPED)
    # No fabricated 429 handling — the message says backoff is skipped.
    assert "backoff" in deg.message or "skip" in deg.message.lower()


def test_full_capability_harness_has_no_telemetry_degradations() -> None:
    plan = evaluate_degradations("claude", _FULL, requested_mode=MODE_SERIAL)
    assert plan.degradations == ()
    assert plan.degraded is False


def test_bare_harness_collects_every_degradation_in_parallel_request() -> None:
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_PARALLEL)
    assert plan.kinds() == {
        DegradationKind.PARALLEL_TO_SERIAL,
        DegradationKind.USAGE_UNAVAILABLE,
        DegradationKind.RATE_LIMIT_SKIPPED,
    }


def test_undeclared_capabilities_default_to_degraded() -> None:
    """An empty capability map (nothing declared) degrades everything."""
    plan = evaluate_degradations("mystery", {}, requested_mode=MODE_PARALLEL)
    assert plan.has(DegradationKind.PARALLEL_TO_SERIAL)
    assert plan.has(DegradationKind.USAGE_UNAVAILABLE)
    assert plan.has(DegradationKind.RATE_LIMIT_SKIPPED)


# ---------------------------------------------------------------------------
# AC3: any degradation is recordable in the ledger / run summary
# ---------------------------------------------------------------------------


def test_to_records_emits_one_structured_record_per_degradation() -> None:
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_PARALLEL)
    records = plan.to_records()
    assert len(records) == len(plan.degradations)
    for record in records:
        assert record["harness"] == "codex"
        assert record["kind"] in {k.value for k in DegradationKind}
        assert record["requested_mode"] == MODE_PARALLEL
        assert record["effective_mode"] == MODE_SERIAL
        assert isinstance(record["message"], str) and record["message"]


def test_to_records_is_empty_for_a_fully_capable_harness() -> None:
    plan = evaluate_degradations("claude", _FULL, requested_mode=MODE_PARALLEL)
    assert plan.to_records() == []


def test_log_lines_one_per_degradation() -> None:
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_PARALLEL)
    assert len(plan.log_lines()) == len(plan.degradations)
    assert all("codex" in line for line in plan.log_lines())


# ---------------------------------------------------------------------------
# Immutability + shape guarantees
# ---------------------------------------------------------------------------


def test_plan_is_frozen() -> None:
    plan = evaluate_degradations("codex", _BARE, requested_mode=MODE_SERIAL)
    assert isinstance(plan, DegradationPlan)
    with pytest.raises(Exception):
        plan.effective_mode = MODE_PARALLEL  # type: ignore[misc]


def test_degradation_is_frozen() -> None:
    deg = Degradation(kind=DegradationKind.USAGE_UNAVAILABLE, message="x")
    with pytest.raises(Exception):
        deg.message = "y"  # type: ignore[misc]


def test_degradation_kind_values_are_stable() -> None:
    """Kind values are persisted to the ledger — they must stay stable strings."""
    assert DegradationKind.PARALLEL_TO_SERIAL.value == "parallel_to_serial"
    assert DegradationKind.USAGE_UNAVAILABLE.value == "usage_unavailable"
    assert DegradationKind.RATE_LIMIT_SKIPPED.value == "rate_limit_skipped"
