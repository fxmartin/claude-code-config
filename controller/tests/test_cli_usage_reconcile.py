# ABOUTME: CLI tests for `sdlc usage-reconcile` — the ledger-vs-logs backfill verb.
# ABOUTME: Story 28.1-001. Covers apply, --dry-run, --all, --json, and clean absence.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

runner = CliRunner()


def _result_line(inp: int, out: int, cost: float) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": "ok",
            "session_id": "sess-1",
            "total_cost_usd": cost,
            "usage": {"input_tokens": inp, "output_tokens": out},
        }
    ) + "\n"


def _turn_line(inp: int, out: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "session_id": "sess-1",
            "message": {"content": [], "usage": {"input_tokens": inp, "output_tokens": out}},
        }
    ) + "\n"


def _seed(tmp_path: Path, log_body: str) -> tuple[Path, str]:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-001", "epic-28", "t", "Must", 5, "python-backend-engineer",
        "feature/28.1-001", None, "DONE",
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")
    logs_dir = Path(f"{db}.logs") / run_id
    logs_dir.mkdir(parents=True)
    (logs_dir / "28.1-001-build-1.log").write_text(log_body, encoding="utf-8")
    return db, run_id


def test_usage_reconcile_backfills_and_summarises(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _result_line(1000, 2000, 3.5))

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.stdout
    assert "28.1-001/build#1" in result.stdout
    assert "1 row(s) updated" in result.stdout
    row = Ledger(db).stage_usage_rows(run_id)[0]
    assert row["input_tokens"] == 1000 and row["cost_usd"] == 3.5


def test_usage_reconcile_dry_run_writes_nothing(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _result_line(1000, 2000, 3.5))

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db), "--dry-run"])

    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert Ledger(db).stage_usage_rows(run_id)[0]["input_tokens"] is None


def test_usage_reconcile_is_idempotent_on_a_second_invocation(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(1000, 2000, 3.5))
    runner.invoke(app, ["usage-reconcile", "--db", str(db)])

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db)])

    assert result.exit_code == 0
    assert "0 row(s) updated" in result.stdout


def test_usage_reconcile_json_shape(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _turn_line(10, 20))

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["agreement_rate"] == 0.0
    assert payload["counts"]["log-recovered"] == 1
    assert payload["updated"][0]["story_id"] == "28.1-001"


def test_usage_reconcile_all_spans_every_run(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(1, 1, 0.1))
    ledger = Ledger(db)
    newer = ledger.run_create("epic-29", "auto")
    ledger.story_upsert(
        newer, "29.1-001", "epic-29", "t", "Must", 3, "qa-engineer",
        "feature/29.1-001", None, "DONE",
    )
    ledger.stage_start(newer, "29.1-001", "build", 1)
    ledger.stage_finish(newer, "29.1-001", "build", 1, "DONE")
    newer_logs = Path(f"{db}.logs") / newer
    newer_logs.mkdir(parents=True)
    (newer_logs / "29.1-001-build-1.log").write_text(_result_line(2, 2, 0.2), "utf-8")

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db), "--all", "--json"])

    payload = json.loads(result.stdout)
    assert {u["story_id"] for u in payload["updated"]} == {"28.1-001", "29.1-001"}


def test_usage_reconcile_reports_unverifiable_rows(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _result_line(1, 1, 0.1))
    for log in (Path(f"{db}.logs") / run_id).iterdir():
        log.unlink()

    result = runner.invoke(app, ["usage-reconcile", "--db", str(db)])

    assert result.exit_code == 0
    assert "unverifiable" in result.stdout
    assert "agreement" not in result.stdout  # no false 100%


def test_usage_reconcile_without_a_ledger_is_clean(tmp_path: Path) -> None:
    result = runner.invoke(app, ["usage-reconcile", "--db", str(tmp_path / "none.db")])
    assert result.exit_code == 0
    assert "no build run found" in result.stdout
    assert not (tmp_path / "none.db").exists()  # never materialise an empty ledger


def test_usage_reconcile_unknown_explicit_run_exits_nonzero(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(1, 1, 0.1))
    result = runner.invoke(app, ["usage-reconcile", "nope", "--db", str(db)])
    assert result.exit_code == 2
