# ABOUTME: Story 34.2-001 — the price-table vintage renders beside `$` figures.
# ABOUTME: Covers the status snapshot/CLI line and the dashboard run header.

from __future__ import annotations

import pytest

from sdlc.build import Ledger, status_snapshot
from sdlc.cost_estimate import PRICE_TABLE_VINTAGE
from sdlc.dashboard import _PAGE


def test_status_snapshot_carries_price_vintage(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    rid = ledger.run_create("epic-34", "serial")
    snap = status_snapshot(ledger, rid)
    assert snap["run"]["price_vintage"] == PRICE_TABLE_VINTAGE


def _seed_stage(ledger, rid, attempt, *, model, cost_usd, **tokens):
    ledger.stage_start(rid, "s1", "build", attempt)
    if model:
        ledger.stage_set_model(rid, "s1", "build", attempt, model)
    ledger.stage_set_usage(
        rid, "s1", "build", attempt, session_id=None, cost_usd=cost_usd,
        input_tokens=tokens.get("input_tokens"),
        output_tokens=tokens.get("output_tokens"),
        cache_read_tokens=tokens.get("cache_read_tokens"),
        cache_creation_tokens=tokens.get("cache_creation_tokens"),
    )


def test_run_cost_prices_usage_rows_from_the_table_per_model(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    rid = ledger.run_create("epic-34", "serial")
    ledger.story_upsert(rid, "s1", "epic-34", "t", "Must", 1, "a", "feature/s1", None, "PENDING")
    # Sonnet 5.5 at 2/10 per Mtok, cache read at 0.20 — the harness's own figure
    # (99.0) is not what the vintage-stamped `$` reports.
    _seed_stage(
        ledger, rid, 1, model="claude-sonnet-5-5", cost_usd=99.0,
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_creation_tokens=0,
    )
    # A cost-only row (no token counts) keeps the harness-reported figure.
    _seed_stage(ledger, rid, 2, model="claude-sonnet-5-5", cost_usd=0.5)
    usage = status_snapshot(ledger, rid)["run"]["usage"]
    assert usage["cost_usd"] == pytest.approx(2 + 10 + 0.20 + 0.5)


def test_dashboard_run_header_renders_vintage_beside_cost() -> None:
    assert "run.price_vintage" in _PAGE
    assert "prices " in _PAGE


def test_status_cli_prints_cost_with_vintage(tmp_path) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    db = tmp_path / "l.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-34", "serial")
    ledger.story_upsert(rid, "s1", "epic-34", "t", "Must", 1, "a", "feature/s1", None, "PENDING")
    ledger.stage_start(rid, "s1", "build", 1)
    ledger.stage_set_usage(
        rid, "s1", "build", 1, session_id=None, input_tokens=7_750,
        output_tokens=10_000, cache_read_tokens=0, cache_creation_tokens=0,
        cost_usd=9.99,  # harness-reported; the table-priced figure is shown
    )
    result = CliRunner().invoke(app, ["status", "--db", str(db), "--run", rid])
    assert f"$0.231 · prices {PRICE_TABLE_VINTAGE}" in result.output


def test_status_cli_omits_cost_line_without_usage(tmp_path) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    db = tmp_path / "l.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-34", "serial")
    result = CliRunner().invoke(app, ["status", "--db", str(db), "--run", rid])
    assert "prices " not in result.output
