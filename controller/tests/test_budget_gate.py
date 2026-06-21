# ABOUTME: Tests for the per-run token budget gate (Story 14.1-001).
# ABOUTME: Token accrual drives a pause/abort gate; $-budgets convert to notional.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import (
    NOTIONAL_USD_PER_MILLION_TOKENS,
    BuildOptions,
    Ledger,
    notional_cost_label,
    parse_build_args,
    run_build,
    usd_to_notional_tokens,
)
from sdlc.resume import run_resume

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue

# Each story runs build+coverage+review+merge under the fake dispatcher, so a
# fully-built story accrues four sample stages' worth of tokens.
_TOKENS_PER_STORY = 4 * _SAMPLE_STAGE_TOKENS


# ---------------------------------------------------------------------------
# Argument parsing — --budget (tokens or $) and --budget-policy
# ---------------------------------------------------------------------------

def test_parse_budget_tokens() -> None:
    opts = parse_build_args(["epic-99", "--budget=50000"])
    assert opts.budget == 50000
    assert opts.budget_usd is None
    assert opts.budget_policy == "pause"  # default policy


def test_parse_budget_with_underscores_and_commas() -> None:
    assert parse_build_args(["--budget=1_000_000"]).budget == 1_000_000
    assert parse_build_args(["--budget=2,500,000"]).budget == 2_500_000


def test_parse_budget_dollars_converts_to_notional_tokens() -> None:
    opts = parse_build_args(["--budget=$15"])
    assert opts.budget_usd == 15.0
    # $15 at the notional rate is exactly one million tokens.
    assert opts.budget == usd_to_notional_tokens(15.0)
    assert opts.budget == 1_000_000


def test_parse_budget_dollars_usd_suffix() -> None:
    opts = parse_build_args(["--budget=30usd"])
    assert opts.budget_usd == 30.0
    assert opts.budget == usd_to_notional_tokens(30.0)


def test_parse_budget_policy_abort() -> None:
    assert parse_build_args(["--budget=10", "--budget-policy=abort"]).budget_policy == "abort"


def test_parse_budget_policy_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_build_args(["--budget-policy=halt"])


def test_parse_budget_negative_raises() -> None:
    with pytest.raises(ValueError):
        parse_build_args(["--budget=-5"])


def test_no_budget_means_zero() -> None:
    opts = parse_build_args(["epic-99"])
    assert opts.budget == 0


# ---------------------------------------------------------------------------
# Notional-$ display helpers
# ---------------------------------------------------------------------------

def test_notional_cost_label_is_clearly_not_real_spend() -> None:
    label = notional_cost_label(1.234)
    assert "$1.23" in label
    assert "API-equivalent" in label
    assert "not billed on subscription" in label


def test_notional_cost_label_handles_none() -> None:
    assert "not billed on subscription" in notional_cost_label(None)


def test_usd_to_notional_tokens_uses_documented_rate() -> None:
    # One rate-unit of dollars buys exactly one million tokens.
    assert usd_to_notional_tokens(NOTIONAL_USD_PER_MILLION_TOKENS) == 1_000_000


# ---------------------------------------------------------------------------
# Ledger.run_usage_totals — the running accrual the gate reads
# ---------------------------------------------------------------------------

def test_run_usage_totals_empty_run_is_zero(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    totals = ledger.run_usage_totals(run_id)
    assert totals == {"tokens": 0, "cost_usd": 0.0}


def test_run_usage_totals_sums_recorded_stages(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 1, "py", "", None, "TODO")
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_usage(
        run_id, "s1-001", "build", 1,
        session_id="sess", input_tokens=100, output_tokens=20,
        cache_read_tokens=4000, cache_creation_tokens=300, cost_usd=0.05,
    )
    totals = ledger.run_usage_totals(run_id)
    assert totals["tokens"] == _SAMPLE_STAGE_TOKENS
    assert totals["cost_usd"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# run_build budget gate — pause / abort / no-budget
# ---------------------------------------------------------------------------

def _run(db, *, budget=0, policy="pause", dispatcher=None):
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True,
        budget=budget, budget_policy=policy,
    )
    return opts, run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher or FakeDispatcher(),
        preflight=lambda: True,
    )


def test_no_budget_path_is_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _opts, result = _run(db, budget=0)
    assert result.completed == 3
    assert result.budget_stopped is False
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def test_budget_pause_stops_dispatching_after_ceiling(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    # Trip the gate after the first story's stages accrue.
    _opts, result = _run(db, budget=10_000, dispatcher=dispatcher)

    assert result.budget_stopped is True
    assert result.budget_policy == "pause"
    assert result.completed == 1
    # Only the first story's stages were ever dispatched.
    dispatched_stories = {sid for _, sid in dispatcher.calls}
    assert dispatched_stories == {"s1-001"}
    # Accrual is surfaced on the result and crossed the ceiling.
    assert result.accrued_tokens >= 10_000
    assert result.accrued_tokens == _TOKENS_PER_STORY


def test_budget_pause_leaves_run_resumable(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _opts, result = _run(db, budget=10_000)
    ledger = Ledger(db)
    # A paused run is NOT stamped terminal — it stays IN_PROGRESS so resume picks it up.
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"
    assert ledger.latest_resumable_run("epic-99") == result.run_id


def test_budget_pause_logs_notional_reason(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _opts, result = _run(db, budget=10_000)
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        msgs = [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ?", (result.run_id,)
            ).fetchall()
        ]
    budget_events = [m for m in msgs if "budget ceiling crossed" in m]
    assert budget_events, "expected a budget-ceiling event in the ledger"
    assert any("not billed on subscription" in m for m in budget_events)


def test_budget_abort_marks_run_terminal(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _opts, result = _run(db, budget=10_000, policy="abort")
    assert result.budget_stopped is True
    assert result.budget_policy == "abort"
    ledger = Ledger(db)
    # Abort is a terminal stop — not resumable.
    assert ledger.run_row(result.run_id)["status"] == "ABORTED"
    assert ledger.latest_resumable_run("epic-99") is None


def test_budget_at_zero_never_gates_even_after_spend(tmp_path: Path) -> None:
    # Belt-and-suspenders: 0 is "no ceiling", never "ceiling of zero".
    db = tmp_path / "ledger.db"
    _opts, result = _run(db, budget=0)
    assert result.budget_stopped is False
    assert result.completed == 3


# ---------------------------------------------------------------------------
# Pause → resume: a raised budget continues the same run to completion
# ---------------------------------------------------------------------------

_SAMPLE_EPIC = """# Epic 88

##### Story 88.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 88.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 88.1-003: Three
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _make_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-88-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def test_budget_pause_then_resume_completes_run(tmp_path: Path) -> None:
    from sdlc.discovery import discover_queue

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-88", tmp_path)
    assert len(queue) == 3

    opts = BuildOptions(
        scope="epic-88", skip_preflight=True, sequential=True, budget=10_000,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.budget_stopped is True
    assert result.completed == 1

    # Raise the budget (here: drop it) and resume — the same run finishes cleanly.
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path
    )
    assert resumed.run_id == result.run_id
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert all(v == "DONE" for v in statuses.values()), statuses
    assert ledger.run_row(result.run_id)["status"] == "DONE"
