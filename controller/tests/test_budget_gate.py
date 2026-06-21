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

from typer.testing import CliRunner

from sdlc.cli import app

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue

runner = CliRunner()

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
    return run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher or FakeDispatcher(),
        preflight=lambda: True,
    )


def test_no_budget_path_is_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, budget=0)
    assert result.completed == 3
    assert result.budget_stopped is False
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def test_budget_pause_stops_dispatching_after_ceiling(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    # Trip the gate after the first story's stages accrue.
    result = _run(db, budget=10_000, dispatcher=dispatcher)

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
    result = _run(db, budget=10_000)
    ledger = Ledger(db)
    # A paused run is NOT stamped terminal — it stays IN_PROGRESS so resume picks it up.
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"
    assert ledger.latest_resumable_run("epic-99") == result.run_id


def test_budget_pause_logs_notional_reason(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, budget=10_000)
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
    result = _run(db, budget=10_000, policy="abort")
    assert result.budget_stopped is True
    assert result.budget_policy == "abort"
    ledger = Ledger(db)
    # Abort is a terminal stop — not resumable.
    assert ledger.run_row(result.run_id)["status"] == "ABORTED"
    assert ledger.latest_resumable_run("epic-99") is None


def test_budget_at_zero_never_gates_even_after_spend(tmp_path: Path) -> None:
    # Belt-and-suspenders: 0 is "no ceiling", never "ceiling of zero".
    db = tmp_path / "ledger.db"
    result = _run(db, budget=0)
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


def _build_paused(tmp_path: Path):
    """Build epic-88 with a tiny budget so it pauses after the first story."""
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
    return db, result


def test_resume_without_raising_budget_repauses(tmp_path: Path) -> None:
    # The bug guard: a budget-paused run must NOT resume unbounded. With the
    # ceiling carried in the ledger config, an un-raised resume re-pauses
    # immediately and dispatches nothing further.
    db, result = _build_paused(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path
    )
    assert resumed.run_id == result.run_id
    assert resumed.budget_stopped is True
    assert resumed.resumed == 0  # nothing dispatched — already over the ceiling
    assert dispatcher.calls == []
    ledger = Ledger(db)
    # Still resumable (pause is not terminal); only the first story is DONE.
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert sum(1 for v in statuses.values() if v == "DONE") == 1


def test_resume_with_raised_budget_completes_run(tmp_path: Path) -> None:
    # "Resumable once the budget is raised": a generous --budget lets the same
    # run finish cleanly.
    db, result = _build_paused(tmp_path)
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        budget=10_000_000,
    )
    assert resumed.run_id == result.run_id
    assert resumed.budget_stopped is False
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert all(v == "DONE" for v in statuses.values()), statuses
    assert ledger.run_row(result.run_id)["status"] == "DONE"


def test_resume_budget_policy_override_aborts_paused_run(tmp_path: Path) -> None:
    # An explicit --budget-policy=abort on resume overrides the persisted policy:
    # the un-raised resume re-trips the ceiling and now stamps the run terminal
    # instead of re-pausing it.
    db, result = _build_paused(tmp_path)
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        budget_policy="abort",
    )
    assert resumed.budget_stopped is True
    assert resumed.budget_policy == "abort"
    ledger = Ledger(db)
    # Abort is terminal — the run is no longer resumable.
    assert ledger.run_row(result.run_id)["status"] == "ABORTED"
    assert ledger.latest_resumable_run("epic-88") is None


def test_resume_repause_invokes_render_view(tmp_path: Path) -> None:
    # The budget-stop close-out on resume regenerates the progress view.
    db, result = _build_paused(tmp_path)
    rendered: list[str] = []
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        render_view=rendered.append,
    )
    assert resumed.budget_stopped is True
    assert rendered == [result.run_id]


# ---------------------------------------------------------------------------
# Multi-cohort dependency chain — the budget stop must skip remaining cohorts
# ---------------------------------------------------------------------------

_CHAINED_EPIC = """# Epic 77

##### Story 77.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 77.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: Story 77.1-001.

##### Story 77.1-003: Three
**Priority**: P1
**Points**: 1
**Dependencies**: Story 77.1-002.
"""


def _build_paused_chained(tmp_path: Path):
    """Build the dependency-chained epic-77 so it pauses after the first cohort."""
    from sdlc.discovery import discover_queue

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-77-sample.md").write_text(_CHAINED_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-77", tmp_path)
    assert len(queue) == 3
    opts = BuildOptions(scope="epic-77", skip_preflight=True, budget=10_000)
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.budget_stopped is True
    assert result.completed == 1
    return db, result


def test_resume_repause_skips_remaining_cohorts(tmp_path: Path) -> None:
    # With a dependency chain the queue splits into three serial cohorts. A
    # re-paused resume trips the ceiling in the second cohort and must break out
    # of the cohort loop entirely rather than fall through to the third.
    db, result = _build_paused_chained(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-77", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path
    )
    assert resumed.budget_stopped is True
    assert resumed.resumed == 0  # nothing dispatched — over the ceiling
    assert dispatcher.calls == []
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    # Only the first cohort's story is DONE; the rest stay TODO (never dispatched).
    assert statuses["77.1-001"] == "DONE"
    assert statuses["77.1-002"] == "TODO"
    assert statuses["77.1-003"] == "TODO"


# ---------------------------------------------------------------------------
# run_build close-out: registry + render_view side-effects on a budget stop
# ---------------------------------------------------------------------------

def test_budget_abort_marks_registry_finished(tmp_path: Path) -> None:
    # An abort-policy budget stop reconciles the registry record to ABORTED so
    # the multi-run overview never shows the halted run as still live.
    from sdlc.registry import Registry

    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True,
        budget=10_000, budget_policy="abort",
    )
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        registry=registry,
    )
    assert result.budget_stopped is True
    records = registry.records()
    assert len(records) == 1
    assert records[0].status == "ABORTED"
    assert records[0].finished_at


def test_budget_close_out_invokes_render_view(tmp_path: Path) -> None:
    # The run_build budget close-out regenerates the markdown progress view.
    db = tmp_path / "ledger.db"
    rendered: list[str] = []
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, budget=10_000,
    )
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        render_view=rendered.append,
    )
    assert result.budget_stopped is True
    assert rendered == [result.run_id]


# ---------------------------------------------------------------------------
# CLI surface: `sdlc resume --budget / --budget-policy`
# ---------------------------------------------------------------------------

def test_cli_resume_rejects_malformed_budget(tmp_path: Path, monkeypatch) -> None:
    # A non-numeric --budget is a usage error (exit 2) with an actionable message.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["resume", "epic-88", "--budget=notanumber"])
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_cli_resume_rejects_invalid_budget_policy(tmp_path: Path, monkeypatch) -> None:
    # An unknown --budget-policy is a usage error (exit 2) before any work runs.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["resume", "epic-88", "--budget-policy=halt"]
    )
    assert result.exit_code == 2
    assert "pause|abort" in result.output


def test_cli_resume_reports_budget_stop(tmp_path: Path, monkeypatch) -> None:
    # End-to-end CLI: a budget-paused run re-pauses on `sdlc resume` (the ceiling
    # trips pre-dispatch so no real agent runs), prints the notional-$ summary,
    # and exits non-zero because the run did not finish cleanly.
    db, _result = _build_paused(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["resume", "epic-88", "--db", str(db)])
    assert result.exit_code == 1
    assert "budget ceiling crossed" in result.output
    assert "not billed on subscription" in result.output
