# ABOUTME: Tests the historical `stages.model` NULL backfill from session logs and the
# ABOUTME: model-coverage scoring both `sdlc model-backfill` and doctor read (28.1-002).

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sdlc.build import Ledger
from sdlc.model_backfill import (
    BACKFILLED,
    NOT_DISPATCHED,
    RECORDED,
    RECOVERABLE,
    UNRECOVERABLE,
    backfill_models,
    parse_log_model,
)

_OPUS = "claude-opus-4-8"
_HAIKU = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Fixtures: a ledger with stage rows + matching transcripts on disk
# ---------------------------------------------------------------------------


def _result_line(model_usage: dict | None, **extra) -> str:
    event = {"type": "result", "subtype": "success", "session_id": "s-1", **extra}
    if model_usage is not None:
        event["modelUsage"] = model_usage
    return json.dumps(event)


def _usage(cost: float = 1.0, output: int = 100) -> dict:
    return {"costUSD": cost, "outputTokens": output, "inputTokens": 10}


def _seed(tmp_path: Path) -> tuple[Ledger, str, Path]:
    """A one-story run whose stage rows all start with a NULL model."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "serial")
    ledger.story_upsert(
        run_id, "28.1-002", "28", "Model recording", "must", 3, "python", "", None, "TODO"
    )
    logs_dir = Path(f"{db}.logs") / run_id
    logs_dir.mkdir(parents=True)
    return ledger, run_id, logs_dir


def _stage(ledger: Ledger, run_id: str, name: str, attempt: int = 1,
           status: str = "DONE", model: str | None = None) -> None:
    ledger.stage_start(run_id, "28.1-002", name, attempt, model=model)
    if status != "IN_PROGRESS":
        ledger.stage_finish(run_id, "28.1-002", name, attempt, status)


def _model_column(ledger: Ledger, stage: str, attempt: int = 1) -> str | None:
    conn = sqlite3.connect(ledger.db_path)
    try:
        row = conn.execute(
            "SELECT model FROM stages WHERE stage_name = ? AND attempt = ?",
            (stage, attempt),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# parse_log_model
# ---------------------------------------------------------------------------


def test_parse_log_model_reads_model_usage(tmp_path: Path) -> None:
    log = tmp_path / "s-build-1.log"
    log.write_text(
        json.dumps({"type": "assistant", "message": {"model": _HAIKU}}) + "\n"
        + _result_line({_OPUS: _usage(11.5, 74189)}) + "\n",
        encoding="utf-8",
    )
    # The result envelope's modelUsage is authoritative over any single turn.
    assert parse_log_model(log) == _OPUS


def test_parse_log_model_picks_the_dominant_of_several(tmp_path: Path) -> None:
    log = tmp_path / "s-build-1.log"
    log.write_text(
        _result_line({_HAIKU: _usage(0.02, 300), _OPUS: _usage(9.9, 40000)}),
        encoding="utf-8",
    )
    assert parse_log_model(log) == _OPUS


def test_parse_log_model_recovers_from_a_crashed_session(tmp_path: Path) -> None:
    """No terminal result line — the assistant turns still name the model."""
    log = tmp_path / "s-build-1.log"
    log.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n"
        + json.dumps({"type": "assistant", "message": {"model": _OPUS}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"model": _OPUS}}) + "\n",
        encoding="utf-8",
    )
    assert parse_log_model(log) == _OPUS


def test_parse_log_model_reads_a_pretty_printed_envelope(tmp_path: Path) -> None:
    log = tmp_path / "s-build-1.log"
    log.write_text(
        json.dumps(
            {"type": "result", "result": "ok", "modelUsage": {_OPUS: _usage()}}, indent=2
        ),
        encoding="utf-8",
    )
    assert parse_log_model(log) == _OPUS


def test_parse_log_model_none_on_a_plain_text_transcript(tmp_path: Path) -> None:
    log = tmp_path / "s-build-1.log"
    log.write_text("just some agent prose\n<<<RESULT_JSON>>>\n{}\n", encoding="utf-8")
    assert parse_log_model(log) is None


def test_parse_log_model_none_on_a_missing_file(tmp_path: Path) -> None:
    assert parse_log_model(tmp_path / "nope.log") is None


# ---------------------------------------------------------------------------
# backfill_models — the NULL backfill itself
# ---------------------------------------------------------------------------


def test_backfill_writes_the_log_model_onto_a_null_row(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build")
    (logs_dir / "28.1-002-build-1.log").write_text(
        _result_line({_OPUS: _usage()}), encoding="utf-8"
    )

    result = backfill_models(ledger, run_id)

    assert _model_column(ledger, "build") == _OPUS
    assert [a.reason for a in result.audits] == [BACKFILLED]
    assert result.updated and result.updated[0].log_model == _OPUS


def test_backfill_leaves_an_unrecoverable_row_null_but_counted(tmp_path: Path) -> None:
    """AC2: never coerced to a placeholder — reported so coverage stays honest."""
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build")
    _stage(ledger, run_id, "coverage")
    (logs_dir / "28.1-002-build-1.log").write_text("plain text\n", encoding="utf-8")
    # coverage has no transcript at all (pruned by `sdlc clean`).

    result = backfill_models(ledger, run_id)

    assert _model_column(ledger, "build") is None
    assert _model_column(ledger, "coverage") is None
    assert result.counts()[UNRECOVERABLE] == 2
    assert result.unrecoverable == 2
    assert result.coverage == 0.0


def test_backfill_leaves_an_already_recorded_model_alone(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build", model="opus")
    (logs_dir / "28.1-002-build-1.log").write_text(
        _result_line({_HAIKU: _usage()}), encoding="utf-8"
    )

    result = backfill_models(ledger, run_id)

    assert _model_column(ledger, "build") == "opus"
    assert [a.reason for a in result.audits] == [RECORDED]
    assert result.updated == []


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build")
    (logs_dir / "28.1-002-build-1.log").write_text(
        _result_line({_OPUS: _usage()}), encoding="utf-8"
    )

    backfill_models(ledger, run_id)
    second = backfill_models(ledger, run_id)

    assert second.updated == []
    assert [a.reason for a in second.audits] == [RECORDED]
    assert _model_column(ledger, "build") == _OPUS


def test_backfill_matches_a_recovery_row_by_glob(tmp_path: Path) -> None:
    """`bugfix` rows key on (role, seq) while the log embeds the origin stage."""
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "bugfix", attempt=1)
    (logs_dir / "28.1-002-bugfix-build-1.log").write_text(
        _result_line({_HAIKU: _usage()}), encoding="utf-8"
    )

    backfill_models(ledger, run_id)

    assert _model_column(ledger, "bugfix") == _HAIKU


def test_backfill_skips_rows_that_were_never_dispatched(tmp_path: Path) -> None:
    """A SKIPPED (docs-only / cost-gated) row has no model *by design*."""
    ledger, run_id, _ = _seed(tmp_path)
    _stage(ledger, run_id, "coverage", status="SKIPPED")

    result = backfill_models(ledger, run_id)

    assert [a.reason for a in result.audits] == [NOT_DISPATCHED]
    assert result.dispatched == 0
    assert result.coverage is None


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build")
    (logs_dir / "28.1-002-build-1.log").write_text(
        _result_line({_OPUS: _usage()}), encoding="utf-8"
    )

    result = backfill_models(ledger, run_id, apply=False)

    assert _model_column(ledger, "build") is None
    assert [a.reason for a in result.audits] == [RECOVERABLE]
    assert result.updated == []
    # Coverage reflects what is recorded *now*, not what could be recovered.
    assert result.coverage == 0.0


def test_coverage_counts_only_dispatched_rows(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build", model=_OPUS)
    _stage(ledger, run_id, "coverage", status="SKIPPED")
    _stage(ledger, run_id, "review")
    (logs_dir / "28.1-002-review-1.log").write_text("plain\n", encoding="utf-8")

    result = backfill_models(ledger, run_id, apply=False)

    assert result.dispatched == 2
    assert result.populated == 1
    assert result.coverage == 0.5


def test_backfill_sweeps_every_run_with_all_runs(tmp_path: Path) -> None:
    ledger, first, first_logs = _seed(tmp_path)
    _stage(ledger, first, "build")
    (first_logs / "28.1-002-build-1.log").write_text(
        _result_line({_OPUS: _usage()}), encoding="utf-8"
    )
    second = ledger.run_create("epic-28", "serial")
    ledger.story_upsert(
        second, "28.1-002", "28", "t", "must", 3, "python", "", None, "TODO"
    )
    ledger.stage_start(second, "28.1-002", "build", 1)
    ledger.stage_finish(second, "28.1-002", "build", 1, "DONE")
    second_logs = Path(f"{ledger.db_path}.logs") / second
    second_logs.mkdir(parents=True)
    (second_logs / "28.1-002-build-1.log").write_text(
        _result_line({_HAIKU: _usage()}), encoding="utf-8"
    )

    result = backfill_models(ledger, all_runs=True)

    assert len(result.run_ids) == 2
    assert len(result.updated) == 2
    assert {a.log_model for a in result.updated} == {_OPUS, _HAIKU}


def test_backfill_defaults_to_the_latest_run(tmp_path: Path) -> None:
    ledger, first, first_logs = _seed(tmp_path)
    _stage(ledger, first, "build")
    (first_logs / "28.1-002-build-1.log").write_text(
        _result_line({_OPUS: _usage()}), encoding="utf-8"
    )

    result = backfill_models(ledger)

    assert result.run_ids == [first]


def test_backfill_on_a_missing_ledger_is_a_no_op(tmp_path: Path) -> None:
    result = backfill_models(Ledger(tmp_path / "absent.db"))
    assert result.audits == []
    assert result.coverage is None


def test_null_rows_are_reported_per_row_for_the_operator(tmp_path: Path) -> None:
    ledger, run_id, logs_dir = _seed(tmp_path)
    _stage(ledger, run_id, "build")
    (logs_dir / "28.1-002-build-1.log").write_text("plain\n", encoding="utf-8")

    payload = backfill_models(ledger, run_id).to_dict()

    assert payload["coverage"] == 0.0
    assert payload["counts"][UNRECOVERABLE] == 1
    assert payload["residual"][0]["stage_name"] == "build"
    assert payload["residual"][0]["reason"] == UNRECOVERABLE
