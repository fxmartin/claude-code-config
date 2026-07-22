# ABOUTME: Tests for the ledger-vs-logs usage reconciliation pass (Story 28.1-001).
# ABOUTME: Covers overwrite-era backfill, crash recovery, idempotency, no-log degradation.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.build import Ledger
from sdlc.usage_reconcile import (
    AGREE,
    LOG_RECOVERED,
    NO_LOG,
    NO_USAGE,
    SOURCE_LOG_RECOVERED,
    SOURCE_LOG_RESULT,
    STILL_DIVERGENT,
    parse_log_usage,
    reconcile_usage,
    resolve_stage_log,
)

# --- fixtures ---------------------------------------------------------------


def _turn(inp: int, out: int, read: int = 0, create: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create,
    }


def _write_log(
    path: Path,
    *,
    turns: list[dict],
    result: dict | None = None,
    session_id: str = "sess-1",
    trailer: str = "",
) -> Path:
    """Write a raw stream-json transcript exactly as dispatch tees it."""
    lines = [json.dumps({"type": "system", "session_id": session_id})]
    for usage in turns:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "message": {"content": [], "usage": usage},
                }
            )
        )
    if result is not None:
        lines.append(json.dumps(result))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" + trailer, encoding="utf-8")
    return path


def _result(usage: dict, cost: float, session_id: str = "sess-1") -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": "<<<RESULT_JSON>>>{}<<<END_RESULT>>>",
        "session_id": session_id,
        "total_cost_usd": cost,
        "usage": usage,
    }


def _seed(tmp_path: Path) -> tuple[Ledger, str, Path]:
    """A ledger with one run + story, plus its per-run transcript dir."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-001", "epic-28", "Reconciliation", "Must", 5,
        "python-backend-engineer", "feature/28.1-001", None, "IN_PROGRESS",
    )
    logs_dir = Path(f"{db}.logs") / run_id
    logs_dir.mkdir(parents=True)
    return ledger, run_id, logs_dir


def _row(ledger: Ledger, run_id: str, stage: str, attempt: int) -> dict:
    for row in ledger.stage_usage_rows(run_id):
        if row["stage_name"] == stage and row["attempt"] == attempt:
            return row
    raise AssertionError(f"no {stage}#{attempt} row")


def _audit(result, stage: str, attempt: int):
    for audit in result.audits:
        if audit.stage_name == stage and audit.attempt == attempt:
            return audit
    raise AssertionError(f"no audit for {stage}#{attempt}")


# --- log parsing ------------------------------------------------------------


def test_parse_log_usage_prefers_the_terminal_result_line(tmp_path):
    """The `result` event is authoritative: its usage + cost win over the turns."""
    log = _write_log(
        tmp_path / "a.log",
        turns=[_turn(10, 20), _turn(30, 40)],
        result=_result(_turn(100, 200, 300, 400), 1.25),
    )
    usage = parse_log_usage(log)
    assert usage is not None
    assert usage.complete is True
    assert usage.total_tokens == 1000
    assert usage.cost_usd == 1.25
    assert usage.session_id == "sess-1"


def test_parse_log_usage_recovers_a_crashed_session_without_cost(tmp_path):
    """Issue #481: no terminal result → sum the streamed turns, cost unavailable."""
    log = _write_log(tmp_path / "a.log", turns=[_turn(10, 20, 5), _turn(30, 40)])
    usage = parse_log_usage(log)
    assert usage is not None
    assert usage.complete is False
    assert usage.total_tokens == 105
    assert usage.cost_usd is None  # never a fabricated dollar figure
    assert usage.session_id == "sess-1"


def test_parse_log_usage_tolerates_a_killed_transcript_trailer(tmp_path):
    """A quarantined/killed transcript's non-JSON trailer is skipped, not fatal."""
    log = _write_log(
        tmp_path / "a.log",
        turns=[_turn(10, 20)],
        trailer="\n--- KILLED (stalled: no output for 300s) ---\n",
    )
    usage = parse_log_usage(log)
    assert usage is not None and usage.total_tokens == 30 and usage.complete is False


def test_parse_log_usage_reads_a_captured_pretty_printed_envelope(tmp_path):
    """The non-streaming path writes the whole JSON envelope, possibly multi-line."""
    log = tmp_path / "a.log"
    log.write_text(json.dumps(_result(_turn(1, 2, 3, 4), 0.5), indent=2), encoding="utf-8")
    usage = parse_log_usage(log)
    assert usage is not None and usage.complete is True and usage.total_tokens == 10


def test_parse_log_usage_returns_none_for_a_usage_free_log(tmp_path):
    """A plain-text transcript (custom agent) carries no usage — nothing to derive."""
    log = tmp_path / "a.log"
    log.write_text("just some prose the agent printed\n", encoding="utf-8")
    assert parse_log_usage(log) is None


def test_parse_log_usage_returns_none_for_a_missing_file(tmp_path):
    assert parse_log_usage(tmp_path / "gone.log") is None


def test_parse_log_usage_ignores_a_non_numeric_cost_field(tmp_path):
    """A malformed `total_cost_usd` reads as *no* cost, never as a coerced figure.

    The whole point of the pass is that the ledger stops carrying invented
    dollars, so a transcript whose cost field is a string (or a bool, which
    Python would otherwise happily treat as 1.0) must degrade to "cost
    unavailable" while its token counts still land.
    """
    event = _result(_turn(10, 20), 0.0)
    event["total_cost_usd"] = "1.25"  # a string, not a number
    usage = parse_log_usage(_write_log(tmp_path / "a.log", turns=[], result=event))
    assert usage is not None and usage.complete is True
    assert usage.total_tokens == 30
    assert usage.cost_usd is None

    event["total_cost_usd"] = True  # a bool is not a dollar figure either
    boolish = parse_log_usage(_write_log(tmp_path / "b.log", turns=[], result=event))
    assert boolish is not None and boolish.cost_usd is None


# --- log resolution ---------------------------------------------------------


def test_resolve_stage_log_matches_recovery_row_naming(tmp_path):
    """reask/bugfix rows key on `(stage_name, attempt)`; the log embeds the stage."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "28.1-001-build-1.log").write_text("", encoding="utf-8")
    (logs / "28.1-001-reask-build-2.log").write_text("", encoding="utf-8")
    (logs / "28.1-001-bugfix-review-3.log").write_text("", encoding="utf-8")
    (logs / "28.1-001-commitlint-4.log").write_text("", encoding="utf-8")

    assert resolve_stage_log(logs, "28.1-001", "build", 1).name == "28.1-001-build-1.log"
    assert resolve_stage_log(logs, "28.1-001", "reask", 2).name == "28.1-001-reask-build-2.log"
    assert resolve_stage_log(logs, "28.1-001", "bugfix", 3).name == "28.1-001-bugfix-review-3.log"
    assert resolve_stage_log(logs, "28.1-001", "commitlint", 4).name == "28.1-001-commitlint-4.log"
    assert resolve_stage_log(logs, "28.1-001", "build", 9) is None


def test_resolve_stage_log_falls_back_to_the_recorded_output_path(tmp_path):
    """A log outside the naming convention is still found via the row's output_path."""
    logs = tmp_path / "logs"
    logs.mkdir()
    elsewhere = tmp_path / "custom.log"
    elsewhere.write_text("", encoding="utf-8")
    found = resolve_stage_log(logs, "28.1-001", "build", 1, output_path=str(elsewhere))
    assert found == elsewhere


def test_resolve_stage_log_disambiguates_an_ambiguous_glob_by_output_path(tmp_path):
    """Two recovery logs can share `(role, attempt)`; output_path breaks the tie.

    A `bugfix` row keys only on its sequence number, so two bugfixes recovering
    different originating stages at the same sequence both match the glob. The
    recorded output_path is trustworthy *here* — it only ever mis-points across
    the row boundary the overwrite bug touched — so it picks the right one, and
    resolution stays deterministic (first sorted match) when it cannot help.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    build = logs / "28.1-001-bugfix-build-1.log"
    review = logs / "28.1-001-bugfix-review-1.log"
    for path in (build, review):
        path.write_text("", encoding="utf-8")

    assert resolve_stage_log(
        logs, "28.1-001", "bugfix", 1, output_path=str(review)
    ) == review
    # No hint, or a hint pointing outside the match set → first sorted match.
    assert resolve_stage_log(logs, "28.1-001", "bugfix", 1) == build
    assert resolve_stage_log(
        logs, "28.1-001", "bugfix", 1, output_path=str(tmp_path / "unrelated.log")
    ) == build


# --- backfill ---------------------------------------------------------------


def test_reconcile_backfills_the_original_expensive_row_not_the_recovery_log(tmp_path):
    """Overwrite-era history: the build row's output_path points at the *reask* log.

    Pre-PR #482 the contract-error recovery overwrote the originating row's
    output_path with the cheap re-ask transcript. Reconciliation must key off the
    row's own `(story, stage, attempt)` naming, so the expensive session's usage
    lands on the build row and the cheap re-ask's usage stays on the reask row.
    """
    ledger, run_id, logs = _seed(tmp_path)
    expensive = _write_log(
        logs / "28.1-001-build-1.log",
        turns=[_turn(500, 500)],
        result=_result(_turn(1000, 2000, 3000, 4000), 4.20),
    )
    cheap = _write_log(
        logs / "28.1-001-reask-build-2.log",
        turns=[],
        result=_result(_turn(10, 20, 30, 40), 0.05, session_id="sess-reask"),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    # The #480 defect: the build row was finished pointing at the re-ask log and
    # carried the *re-ask's* cheap usage.
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE", output_path=str(cheap))
    ledger.stage_set_usage(
        run_id, "28.1-001", "build", 1, session_id="sess-reask",
        input_tokens=10, output_tokens=20, cache_read_tokens=30,
        cache_creation_tokens=40, cost_usd=0.05,
    )
    ledger.stage_start(run_id, "28.1-001", "reask", 2)
    ledger.stage_finish(run_id, "28.1-001", "reask", 2, "DONE", output_path=str(cheap))

    result = reconcile_usage(ledger, run_id)

    build = _row(ledger, run_id, "build", 1)
    assert build["input_tokens"] == 1000
    assert build["cache_creation_tokens"] == 4000
    assert build["cost_usd"] == 4.20
    assert build["session_id"] == "sess-1"
    assert build["usage_source"] == SOURCE_LOG_RESULT
    assert build["output_path"] == str(cheap)  # untouched — usage-only pass
    assert expensive.exists()

    reask = _row(ledger, run_id, "reask", 2)
    assert reask["input_tokens"] == 10 and reask["cost_usd"] == 0.05

    assert _audit(result, "build", 1).updated is True
    assert _audit(result, "build", 1).reason == AGREE
    assert result.agreement_rate == 1.0


def test_reconcile_recovers_crash_session_tokens_and_flags_cost_unavailable(tmp_path):
    """Issue #481: a log with no result line yields tokens only, marked recovered."""
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(logs / "28.1-001-build-1.log", turns=[_turn(100, 200, 300), _turn(1, 2)])
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "FAILED", "dispatch-error")

    result = reconcile_usage(ledger, run_id)

    row = _row(ledger, run_id, "build", 1)
    assert row["input_tokens"] == 101
    assert row["output_tokens"] == 202
    assert row["cache_read_tokens"] == 300
    assert row["cost_usd"] is None  # cost flagged unavailable, never fabricated
    assert row["usage_source"] == SOURCE_LOG_RECOVERED

    audit = _audit(result, "build", 1)
    assert audit.reason == LOG_RECOVERED and audit.updated is True
    # A cost-less row cannot fully match ground truth, so it is a residual, not
    # an agreement.
    assert result.agreement_rate == 0.0
    assert [a.reason for a in result.residual] == [LOG_RECOVERED]


def test_reconcile_is_idempotent_and_never_double_counts(tmp_path):
    """A second (and third) pass rewrites nothing and re-sums nothing."""
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log",
        turns=[_turn(10, 20)],
        result=_result(_turn(100, 200, 300, 400), 1.0),
    )
    _write_log(logs / "28.1-001-review-1.log", turns=[_turn(7, 8)])
    for stage in ("build", "review"):
        ledger.stage_start(run_id, "28.1-001", stage, 1)
        ledger.stage_finish(run_id, "28.1-001", stage, 1, "DONE")

    first = reconcile_usage(ledger, run_id)
    assert len(first.updated) == 2
    before = ledger.stage_usage_rows(run_id)

    second = reconcile_usage(ledger, run_id)
    third = reconcile_usage(ledger, run_id)

    assert second.updated == [] and third.updated == []
    assert ledger.stage_usage_rows(run_id) == before
    assert _row(ledger, run_id, "review", 1)["output_tokens"] == 8  # not 16
    assert second.agreement_rate == 0.5  # build agrees; review is log-recovered


def test_reconcile_skips_in_progress_rows(tmp_path):
    """A stage still running is left alone — its log is not yet final."""
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log",
        turns=[_turn(10, 20)],
        result=_result(_turn(100, 200), 1.0),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)  # never finished

    result = reconcile_usage(ledger, run_id)

    assert result.skipped_in_progress == 1
    assert result.audits == []
    assert _row(ledger, run_id, "build", 1)["input_tokens"] is None


def test_reconcile_recovers_a_crashed_attempt_left_in_progress_by_a_dead_run(tmp_path):
    """The Issue #481 shape: the controller died, so the row never left IN_PROGRESS.

    An IN_PROGRESS stage row under a *terminal* run is not running — it is the
    crashed attempt itself, and skipping it would make crash recovery inert.
    """
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(logs / "28.1-001-review-1.log", turns=[_turn(1000, 2000, 3000)])
    ledger.stage_start(run_id, "28.1-001", "review", 1)  # never finished
    ledger.run_update_status(run_id, "FAILED")

    result = reconcile_usage(ledger, run_id)

    assert result.skipped_in_progress == 0
    row = _row(ledger, run_id, "review", 1)
    assert row["input_tokens"] == 1000 and row["cache_read_tokens"] == 3000
    assert row["cost_usd"] is None
    assert row["usage_source"] == SOURCE_LOG_RECOVERED
    assert _audit(result, "review", 1).reason == LOG_RECOVERED


def test_reconcile_degrades_to_unverifiable_when_logs_are_pruned(tmp_path):
    """No transcript on disk → `no-log`, excluded from the rate, never false agreement."""
    ledger, run_id, _ = _seed(tmp_path)
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")
    ledger.stage_set_usage(
        run_id, "28.1-001", "build", 1, session_id="s",
        input_tokens=1, output_tokens=2, cache_read_tokens=None,
        cache_creation_tokens=None, cost_usd=0.1,
    )

    result = reconcile_usage(ledger, run_id)

    assert _audit(result, "build", 1).reason == NO_LOG
    assert result.verifiable == 0
    assert result.agreement_rate is None
    assert result.unverifiable == 1
    # Untouched: an unverifiable row is never rewritten.
    assert _row(ledger, run_id, "build", 1)["input_tokens"] == 1


def test_reconcile_reports_a_usage_free_log_as_unverifiable(tmp_path):
    ledger, run_id, logs = _seed(tmp_path)
    (logs / "28.1-001-build-1.log").write_text("plain text\n", encoding="utf-8")
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")

    result = reconcile_usage(ledger, run_id)

    assert _audit(result, "build", 1).reason == NO_USAGE
    assert result.agreement_rate is None and result.unverifiable == 1


def test_reconcile_dry_run_reports_divergence_without_writing(tmp_path):
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log",
        turns=[],
        result=_result(_turn(100, 200, 300, 400), 1.0),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")

    result = reconcile_usage(ledger, run_id, apply=False)

    assert _audit(result, "build", 1).reason == STILL_DIVERGENT
    assert result.updated == [] and result.agreement_rate == 0.0
    assert _row(ledger, run_id, "build", 1)["input_tokens"] is None


def test_reconcile_leaves_an_already_agreeing_row_alone(tmp_path):
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log",
        turns=[],
        result=_result(_turn(100, 200, 300, 400), 1.0),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")
    ledger.stage_set_usage(
        run_id, "28.1-001", "build", 1, session_id="sess-1",
        input_tokens=100, output_tokens=200, cache_read_tokens=300,
        cache_creation_tokens=400, cost_usd=1.0,
    )

    result = reconcile_usage(ledger, run_id)

    assert _audit(result, "build", 1).reason == AGREE
    assert result.updated == [] and result.agreement_rate == 1.0
    # A live-recorded row keeps its NULL source — it was never log-derived.
    assert _row(ledger, run_id, "build", 1)["usage_source"] is None


def test_reconcile_backfills_a_row_whose_cost_was_never_recorded(tmp_path):
    """Matching tokens are not agreement when the ledger has no cost at all.

    A row can carry the right token counts and still be missing the dollars the
    completed session actually cost — the estimator trains on cost, so an absent
    figure is drift, not a match, and the log's authoritative cost is written.
    """
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log",
        turns=[],
        result=_result(_turn(100, 200, 300, 400), 2.5),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")
    ledger.stage_set_usage(
        run_id, "28.1-001", "build", 1, session_id="sess-1",
        input_tokens=100, output_tokens=200, cache_read_tokens=300,
        cache_creation_tokens=400, cost_usd=None,
    )

    result = reconcile_usage(ledger, run_id)

    row = _row(ledger, run_id, "build", 1)
    assert row["cost_usd"] == 2.5
    assert row["input_tokens"] == 100  # tokens untouched — they already agreed
    assert row["usage_source"] == SOURCE_LOG_RESULT
    assert _audit(result, "build", 1).updated is True
    assert reconcile_usage(ledger, run_id).updated == []  # and it settles


def test_reconcile_defaults_to_the_latest_run_and_spans_all_with_all_runs(tmp_path):
    ledger, run_id, logs = _seed(tmp_path)
    _write_log(
        logs / "28.1-001-build-1.log", turns=[],
        result=_result(_turn(1, 1), 0.1),
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")

    # A second, newer run — the default scope must see only this one.
    newer = ledger.run_create("epic-29", "auto")
    ledger.story_upsert(
        newer, "29.1-001", "epic-29", "Newer", "Must", 3, "qa-engineer",
        "feature/29.1-001", None, "DONE",
    )
    newer_logs = Path(f"{ledger.db_path}.logs") / newer
    _write_log(
        newer_logs / "29.1-001-build-1.log", turns=[],
        result=_result(_turn(2, 2), 0.2),
    )
    ledger.stage_start(newer, "29.1-001", "build", 1)
    ledger.stage_finish(newer, "29.1-001", "build", 1, "DONE")

    latest_only = reconcile_usage(ledger, apply=False)
    assert {a.story_id for a in latest_only.audits} == {"29.1-001"}

    everything = reconcile_usage(ledger, all_runs=True, apply=False)
    assert {a.story_id for a in everything.audits} == {"28.1-001", "29.1-001"}


def test_reconcile_on_an_absent_ledger_is_a_clean_no_op(tmp_path):
    result = reconcile_usage(Ledger(tmp_path / "missing.db"))
    assert result.audits == [] and result.run_ids == []
    assert result.agreement_rate is None
