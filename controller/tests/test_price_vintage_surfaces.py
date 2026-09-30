# ABOUTME: Story 34.2-001 — the price-table vintage renders beside `$` figures.
# ABOUTME: Covers the status snapshot/CLI line and the dashboard run header.

from __future__ import annotations

from sdlc.build import Ledger, status_snapshot
from sdlc.cost_estimate import PRICE_TABLE_VINTAGE
from sdlc.dashboard import _PAGE


def test_status_snapshot_carries_price_vintage(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    rid = ledger.run_create("epic-34", "serial")
    snap = status_snapshot(ledger, rid)
    assert snap["run"]["price_vintage"] == PRICE_TABLE_VINTAGE


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
        rid, "s1", "build", 1, session_id=None, input_tokens=10, output_tokens=5,
        cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.231,
    )
    result = CliRunner().invoke(app, ["status", "--db", str(db), "--run", rid])
    assert f"$0.231 · prices {PRICE_TABLE_VINTAGE}" in result.output
