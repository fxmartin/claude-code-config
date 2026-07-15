# ABOUTME: Tests for the pre-dispatch cost estimate + warning/gate (Story 14.1-002).
# ABOUTME: Heuristic estimate, threshold warn/gate, and estimate-vs-actual reconcile.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    _estimate_stage_cost,
    _model_price_key,
    _reconcile_estimate,
    _result_total_tokens,
    parse_build_args,
    run_build,
)
from sdlc.dispatch import AgentResult
from sdlc.cost_estimate import (
    DEFAULT_STAGE_FACTOR,
    DEFAULT_STAGE_FACTORS,
    DEFAULT_USD_PER_MILLION_TOKENS,
    MODEL_USD_PER_MILLION_TOKENS,
    CostEstimateConfig,
    StageEstimate,
    estimate_prompt_tokens,
    estimate_stage,
    notional_cost,
)

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue


# ---------------------------------------------------------------------------
# estimate_prompt_tokens — the chars/token heuristic
# ---------------------------------------------------------------------------

def test_prompt_tokens_uses_chars_per_token() -> None:
    # 40 characters at the default ~4 chars/token ≈ 10 tokens.
    assert estimate_prompt_tokens("x" * 40) == 10


def test_prompt_tokens_empty_is_zero() -> None:
    assert estimate_prompt_tokens("") == 0


def test_prompt_tokens_short_is_at_least_one() -> None:
    # A non-empty prompt never estimates as zero tokens.
    assert estimate_prompt_tokens("hi") == 1


def test_prompt_tokens_honours_config_chars_per_token() -> None:
    assert estimate_prompt_tokens("x" * 40, chars_per_token=8) == 5


# ---------------------------------------------------------------------------
# notional_cost — token → notional-$ at the documented rate
# ---------------------------------------------------------------------------

def test_notional_cost_uses_documented_rate() -> None:
    # One million tokens costs exactly the per-million rate.
    assert notional_cost(1_000_000) == pytest.approx(DEFAULT_USD_PER_MILLION_TOKENS)


def test_notional_cost_scales_linearly() -> None:
    assert notional_cost(500_000) == pytest.approx(DEFAULT_USD_PER_MILLION_TOKENS / 2)


# ---------------------------------------------------------------------------
# estimate_stage — factor map, floor, calibration, config override
# ---------------------------------------------------------------------------

def test_estimate_stage_applies_stage_factor() -> None:
    prompt = "x" * 400  # 100 prompt tokens
    est = estimate_stage("build", prompt)
    assert isinstance(est, StageEstimate)
    assert est.prompt_tokens == 100
    assert est.estimated_tokens == int(round(100 * DEFAULT_STAGE_FACTORS["build"]))
    assert est.calibrated is False


def test_estimate_stage_unknown_stage_uses_default_factor() -> None:
    prompt = "x" * 400  # 100 prompt tokens
    est = estimate_stage("nonsense-stage", prompt)
    assert est.estimated_tokens == int(round(100 * DEFAULT_STAGE_FACTOR))


def test_estimate_stage_never_below_prompt_tokens() -> None:
    # A tiny factor must not produce an estimate smaller than the prompt itself.
    cfg = CostEstimateConfig(stage_factors={"merge": 0.1}, default_factor=0.1)
    est = estimate_stage("merge", "x" * 400, config=cfg)
    assert est.estimated_tokens == est.prompt_tokens == 100


def test_estimate_stage_calibrates_from_history() -> None:
    # A historical per-stage average overrides the crude factor and is flagged.
    est = estimate_stage("build", "x" * 400, historical_tokens=7777.0)
    assert est.estimated_tokens == 7777
    assert est.calibrated is True


def test_estimate_stage_ignores_zero_history() -> None:
    # Zero/None history falls back to the heuristic (not a 0-token estimate).
    est = estimate_stage("build", "x" * 400, historical_tokens=0.0)
    assert est.calibrated is False
    assert est.estimated_tokens > 0


def test_estimate_stage_config_overrides_price() -> None:
    cfg = CostEstimateConfig(usd_per_million_tokens=30.0)
    est = estimate_stage("merge", "x" * 4000, config=cfg)
    assert est.estimated_cost_usd == pytest.approx(
        notional_cost(est.estimated_tokens, usd_per_million_tokens=30.0)
    )


# ---------------------------------------------------------------------------
# Ledger: estimate columns + historical calibration query
# ---------------------------------------------------------------------------

def _stage_row(ledger: Ledger, run_id: str, story_id: str, stage: str) -> dict:
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the row directly
        row = conn.execute(
            "SELECT estimated_tokens, estimated_cost_usd FROM stages "
            "WHERE run_id=? AND story_id=? AND stage_name=?",
            (run_id, story_id, stage),
        ).fetchone()
    return dict(row) if row else {}


def test_stage_set_estimate_persists(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 1, "py", "", None, "TODO")
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_estimate(
        run_id, "s1-001", "build", 1, estimated_tokens=4242, estimated_cost_usd=0.06
    )
    row = _stage_row(ledger, run_id, "s1-001", "build")
    assert row["estimated_tokens"] == 4242
    assert row["estimated_cost_usd"] == pytest.approx(0.06)


def test_historical_stage_tokens_averages_done_usage(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 1, "py", "", None, "TODO")
    # Two DONE build attempts with recorded usage → averaged.
    for attempt, out in ((1, 100), (2, 300)):
        ledger.stage_start(run_id, "s1-001", "build", attempt)
        ledger.stage_finish(run_id, "s1-001", "build", attempt, "DONE")
        ledger.stage_set_usage(
            run_id, "s1-001", "build", attempt,
            session_id="s", input_tokens=out, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.01,
        )
    # No harness/model filter → the widest "any" cohort (the pre-#427 average).
    avg, tier = ledger.historical_stage_tokens("build")
    assert avg == pytest.approx(200.0)
    assert tier == "any"


def test_historical_stage_tokens_none_without_usage(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 1, "py", "", None, "TODO")
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")  # no usage recorded
    assert ledger.historical_stage_tokens("build") is None
    assert ledger.historical_stage_tokens("review") is None


# ---------------------------------------------------------------------------
# Issue #427: harness+model-aware calibration ladder
# ---------------------------------------------------------------------------

def _done_stage(
    ledger: Ledger,
    run_id: str,
    story_id: str,
    stage: str,
    attempt: int,
    total_tokens: int,
    *,
    harness: str = "claude",
    model: str | None = None,
) -> None:
    """Record one DONE stage attempt with a single-component usage total."""
    ledger.stage_start(run_id, story_id, stage, attempt, harness=harness, model=model)
    ledger.stage_finish(run_id, story_id, stage, attempt, "DONE")
    ledger.stage_set_usage(
        run_id, story_id, stage, attempt,
        session_id="s", input_tokens=total_tokens, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.01,
    )


def _ledger_with_story(tmp_path: Path) -> tuple[Ledger, str]:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 1, "py", "", None, "TODO")
    return ledger, run_id


def test_historical_tokens_harness_model_tier_wins(tmp_path: Path) -> None:
    # A same-harness+model cohort exists → it wins over the looser cohorts, and the
    # matched tier is reported.
    ledger, run_id = _ledger_with_story(tmp_path)
    # Matching cohort: claude+opus.
    _done_stage(ledger, run_id, "s1-001", "build", 1, 500, harness="claude", model="opus")
    _done_stage(ledger, run_id, "s1-001", "build", 2, 700, harness="claude", model="opus")
    # Noise in other cohorts that must NOT dilute the harness+model average.
    _done_stage(ledger, run_id, "s1-001", "build", 3, 9000, harness="claude", model="haiku")
    _done_stage(ledger, run_id, "s1-001", "build", 4, 9000, harness="codex", model="gpt-5")
    avg, tier = ledger.historical_stage_tokens("build", harness="claude", model="opus")
    assert avg == pytest.approx(600.0)
    assert tier == "harness+model"


def test_historical_tokens_harness_only_fallback(tmp_path: Path) -> None:
    # No row matches the requested model, but the harness has history → fall back to
    # the harness cohort (any model).
    ledger, run_id = _ledger_with_story(tmp_path)
    _done_stage(ledger, run_id, "s1-001", "build", 1, 400, harness="claude", model="sonnet")
    _done_stage(ledger, run_id, "s1-001", "build", 2, 600, harness="claude", model="haiku")
    _done_stage(ledger, run_id, "s1-001", "build", 3, 9000, harness="codex", model="gpt-5")
    avg, tier = ledger.historical_stage_tokens("build", harness="claude", model="opus")
    assert avg == pytest.approx(500.0)  # sonnet+haiku claude rows, codex excluded
    assert tier == "harness"


def test_historical_tokens_any_history_fallback(tmp_path: Path) -> None:
    # Neither the harness nor the model match, but the stage has *some* history →
    # the widest "any" cohort serves it.
    ledger, run_id = _ledger_with_story(tmp_path)
    _done_stage(ledger, run_id, "s1-001", "build", 1, 100, harness="codex", model="gpt-5")
    _done_stage(ledger, run_id, "s1-001", "build", 2, 300, harness="codex", model="gpt-5")
    avg, tier = ledger.historical_stage_tokens("build", harness="claude", model="opus")
    assert avg == pytest.approx(200.0)
    assert tier == "any"


def test_historical_tokens_empty_ledger_is_none(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path)
    assert ledger.historical_stage_tokens("build", harness="claude", model="opus") is None


def test_historical_tokens_backward_compat_null_rows(tmp_path: Path) -> None:
    # Pre-Migration-11 rows carry NULL harness/model. They must neither crash nor
    # pollute the (harness+model)/(harness) cohorts, yet still serve the any rung.
    ledger, run_id = _ledger_with_story(tmp_path)
    _done_stage(ledger, run_id, "s1-001", "build", 1, 1000, harness=None, model=None)  # type: ignore[arg-type]
    _done_stage(ledger, run_id, "s1-001", "build", 2, 3000, harness=None, model=None)  # type: ignore[arg-type]
    # Harness+model and harness cohorts see no matching (non-NULL) rows → fall to any.
    avg, tier = ledger.historical_stage_tokens("build", harness="claude", model="opus")
    assert avg == pytest.approx(2000.0)
    assert tier == "any"


# ---------------------------------------------------------------------------
# Issue #427: per-model blended rate table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("haiku", MODEL_USD_PER_MILLION_TOKENS["haiku"]),
        ("sonnet", MODEL_USD_PER_MILLION_TOKENS["sonnet"]),
        ("opus", MODEL_USD_PER_MILLION_TOKENS["opus"]),
        ("claude-opus-4-8", MODEL_USD_PER_MILLION_TOKENS["opus"]),  # full id normalises
        ("gpt-5-codex", DEFAULT_USD_PER_MILLION_TOKENS),  # unknown → default
        (None, DEFAULT_USD_PER_MILLION_TOKENS),  # routing off → default
        ("", DEFAULT_USD_PER_MILLION_TOKENS),
    ],
)
def test_model_rate_lookup(model, expected) -> None:
    rate = MODEL_USD_PER_MILLION_TOKENS.get(
        _model_price_key(model), DEFAULT_USD_PER_MILLION_TOKENS
    )
    assert rate == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Issue #427: _estimate_stage_cost drives the resolved rate + names the tier
# ---------------------------------------------------------------------------

def _story() -> object:
    from test_build import _sample_queue

    return _sample_queue()[0]


def test_estimate_stage_cost_uses_model_rate(tmp_path: Path) -> None:
    # A haiku dispatch prices its estimate at the haiku blended rate, not the flat
    # opus-equivalent default — proving a non-default config reaches estimate_stage.
    ledger, run_id = _ledger_with_story(tmp_path)
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    est = _estimate_stage_cost(
        "build", _story(), opts, None, ledger, run_id, 1,
        harness="claude", model="haiku",
    )
    assert est is not None
    assert est.estimated_cost_usd == pytest.approx(
        notional_cost(
            est.estimated_tokens,
            usd_per_million_tokens=MODEL_USD_PER_MILLION_TOKENS["haiku"],
        )
    )


def test_estimate_stage_cost_logs_matched_tier(tmp_path: Path) -> None:
    # With same-harness+model history present, the calibration suffix names the
    # matched cohort tier.
    ledger, run_id = _ledger_with_story(tmp_path)
    _done_stage(ledger, run_id, "s1-001", "build", 1, 5000, harness="claude", model="opus")
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    est = _estimate_stage_cost(
        "build", _story(), opts, None, ledger, run_id, 2,
        harness="claude", model="opus",
    )
    assert est is not None and est.calibrated is True
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        msgs = [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id=?", (run_id,)
            ).fetchall()
        ]
    assert any("[calibrated from history: harness+model]" in m for m in msgs)


# ---------------------------------------------------------------------------
# Issue #427: Migration 11 — the stages.model column
# ---------------------------------------------------------------------------

def _stage_columns(ledger: Ledger) -> set[str]:
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test inspects the schema
        return {r["name"] for r in conn.execute("PRAGMA table_info(stages)").fetchall()}


def test_fresh_db_has_model_column(tmp_path: Path) -> None:
    ledger, _ = _ledger_with_story(tmp_path)
    assert "model" in _stage_columns(ledger)


def test_stage_start_persists_model(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path)
    ledger.stage_start(run_id, "s1-001", "build", 1, harness="codex", model="gpt-5-codex")
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the row
        row = conn.execute(
            "SELECT harness, model FROM stages WHERE run_id=? AND story_id=?",
            (run_id, "s1-001"),
        ).fetchone()
    assert row["harness"] == "codex"
    assert row["model"] == "gpt-5-codex"


def test_existing_db_migrates_model_column_additively(tmp_path: Path) -> None:
    # A ledger created without the model column (simulated by dropping it) gains it
    # via ensure_migrated without disturbing existing rows.
    import sqlite3

    ledger, run_id = _ledger_with_story(tmp_path)
    _done_stage(ledger, run_id, "s1-001", "build", 1, 1234, harness="claude", model=None)
    db = tmp_path / "ledger.db"
    # Simulate a pre-Migration-11 ledger: physically remove the column + its record.
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE stages DROP COLUMN model")
        conn.execute("DELETE FROM _migrations WHERE version = 11")
    assert "model" not in _stage_columns(Ledger(db))
    reopened = Ledger(db)
    reopened.ensure_migrated()
    assert "model" in _stage_columns(reopened)
    # The pre-existing usage row survives the additive migration.
    avg, _tier = reopened.historical_stage_tokens("build")
    assert avg == pytest.approx(1234.0)


# ---------------------------------------------------------------------------
# Argument parsing — --cost-threshold (tokens or $)
# ---------------------------------------------------------------------------

def test_parse_cost_threshold_tokens() -> None:
    assert parse_build_args(["epic-99", "--cost-threshold=50000"]).cost_estimate_threshold == 50000


def test_parse_cost_threshold_dollars_converts() -> None:
    opts = parse_build_args(["--cost-threshold=$15"])
    # $15 at the notional rate is one million tokens (same convenience as --budget).
    assert opts.cost_estimate_threshold == 1_000_000


def test_no_cost_threshold_is_zero() -> None:
    assert parse_build_args(["epic-99"]).cost_estimate_threshold == 0


def test_parse_cost_threshold_negative_raises() -> None:
    with pytest.raises(ValueError):
        parse_build_args(["--cost-threshold=-1"])


# ---------------------------------------------------------------------------
# run_build integration: record, warn, gate, reconcile
# ---------------------------------------------------------------------------

def _run(db, *, threshold=0, auto=True, dispatcher=None):
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, auto=auto,
        cost_estimate_threshold=threshold,
    )
    return run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher or FakeDispatcher(),
        preflight=lambda: True,
    )


def _events(db, run_id) -> list[str]:
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        return [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id=?", (run_id,)
            ).fetchall()
        ]


def test_estimate_recorded_on_every_stage(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, threshold=0)
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT estimated_tokens, estimated_cost_usd FROM stages "
            "WHERE run_id=? AND status='DONE'",
            (result.run_id,),
        ).fetchall()
    assert rows, "expected DONE stage rows"
    assert all(r["estimated_tokens"] is not None for r in rows)
    assert all(r["estimated_cost_usd"] is not None for r in rows)


def test_pre_dispatch_estimate_event_logged(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, threshold=0)
    assert any("pre-dispatch estimate" in m for m in _events(db, result.run_id))


def test_reconciliation_event_logged(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, threshold=0)
    recon = [m for m in _events(db, result.run_id) if "estimate reconciled" in m]
    assert recon, "expected an estimate-vs-actual reconciliation event"
    # The actual figure from the fake dispatcher's usage envelope is surfaced.
    assert any(str(_SAMPLE_STAGE_TOKENS) in m for m in recon)


def test_no_threshold_completes_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    result = _run(db, threshold=0)
    assert result.completed == 3
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def test_over_threshold_auto_warns_but_proceeds(tmp_path: Path) -> None:
    # A trivially-low threshold trips on every stage; --auto warns and proceeds.
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    result = _run(db, threshold=1, auto=True, dispatcher=dispatcher)
    assert result.completed == 3
    assert dispatcher.calls, "stages should still dispatch under --auto"
    assert any("exceeds" in m and "threshold" in m for m in _events(db, result.run_id))


def test_over_threshold_interactive_gates(tmp_path: Path) -> None:
    # Interactive (not --auto): an over-threshold estimate halts before dispatch
    # and pauses the run resumably (IN_PROGRESS, not a terminal park).
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    result = _run(db, threshold=1, auto=False, dispatcher=dispatcher)
    assert dispatcher.calls == [], "no agent should run when the cost gate halts"
    assert result.cost_gated is True
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert statuses["s1-001"] == "NEEDS_ATTENTION"
    # The run stays IN_PROGRESS so `sdlc resume` can pick it up — not terminal.
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"
    assert any("gated pre-dispatch" in m for m in _events(db, result.run_id))


# ---------------------------------------------------------------------------
# Resumability: the gate is persisted (no silent bypass) and resumable
# ---------------------------------------------------------------------------

_GATE_EPIC = """# Epic 66

##### Story 66.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _build_cost_gated(tmp_path: Path):
    """Build epic-66 interactively with a trivially-low threshold so it gates."""
    from sdlc.discovery import discover_queue

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-66-sample.md").write_text(_GATE_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-66", tmp_path)
    assert len(queue) == 1
    opts = BuildOptions(
        scope="epic-66", skip_preflight=True, sequential=True, auto=False,
        cost_estimate_threshold=1,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.cost_gated is True
    return db, result


def test_cost_gate_leaves_run_resumable(tmp_path: Path) -> None:
    db, result = _build_cost_gated(tmp_path)
    ledger = Ledger(db)
    # The gated run is NOT stamped terminal — it is discoverable by resume.
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"
    assert ledger.latest_resumable_run("epic-66") == result.run_id


def test_resume_without_raising_threshold_regates(tmp_path: Path) -> None:
    # The silent-bypass guard: the threshold is persisted in the run config, so an
    # un-raised resume re-enforces the gate and dispatches nothing — it does NOT
    # rebuild opts with threshold=0 and silently run the gated stage.
    from sdlc.resume import run_resume

    db, result = _build_cost_gated(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-66", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path
    )
    assert resumed.run_id == result.run_id
    assert resumed.cost_gated is True
    assert dispatcher.calls == [], "the persisted gate must re-halt, not bypass"
    assert Ledger(db).run_row(result.run_id)["status"] == "IN_PROGRESS"


def test_resume_with_cleared_threshold_completes(tmp_path: Path) -> None:
    # Raising/clearing the threshold on resume lets the gated stage proceed —
    # that is how a gated story is continued.
    from sdlc.resume import run_resume

    db, result = _build_cost_gated(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-66", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        cost_threshold=0,  # 0 disables the gate for this resume
    )
    assert resumed.cost_gated is False
    assert dispatcher.calls, "the cleared gate must let the stage dispatch"
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert all(v == "DONE" for v in statuses.values()), statuses
    assert ledger.run_row(result.run_id)["status"] == "DONE"


# ---------------------------------------------------------------------------
# Resumed --auto build keeps its warn-and-proceed posture (no wrong gating)
# ---------------------------------------------------------------------------

_AUTO_EPIC = """# Epic 55

##### Story 55.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 55.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 55.1-003: Three
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def test_config_roundtrip_persists_auto_and_threshold(tmp_path: Path) -> None:
    # The run config carries `auto` + the threshold so resume reconstructs the
    # exact cost-gate posture instead of defaulting to interactive/threshold-0.
    from sdlc.discovery import discover_queue
    from sdlc.resume import _options_from_config

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-55.md").write_text(_AUTO_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-55", tmp_path)
    opts = BuildOptions(
        scope="epic-55", skip_preflight=True, sequential=True, auto=True,
        cost_estimate_threshold=1, budget=10_000,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    ledger = Ledger(db)
    config = ledger.run_config(result.run_id)
    assert config.get("auto") is True
    assert config.get("cost_estimate_threshold") == 1
    rebuilt = _options_from_config("epic-55", ledger.run_row(result.run_id), config)
    assert rebuilt.auto is True
    assert rebuilt.cost_estimate_threshold == 1


def test_resumed_auto_build_does_not_trip_interactive_gate(tmp_path: Path) -> None:
    # The regression: an --auto run warns-and-proceeds over the threshold. After an
    # interruption (here a budget pause after story 1), resume must KEEP auto — not
    # flip to interactive and gate stories the original auto run would have run.
    from sdlc.discovery import discover_queue
    from sdlc.resume import run_resume

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-55.md").write_text(_AUTO_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-55", tmp_path)
    opts = BuildOptions(
        scope="epic-55", skip_preflight=True, sequential=True, auto=True,
        cost_estimate_threshold=1, budget=10_000,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    # The interruption is the budget pause, NOT the cost gate (auto proceeded).
    assert result.budget_stopped is True
    assert result.cost_gated is False

    # Resume with the budget raised; the cost gate must NOT trip because auto was
    # persisted/restored — stories 2 & 3 proceed (warn) to completion.
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-55", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        budget=10_000_000,
    )
    assert resumed.cost_gated is False
    assert dispatcher.calls, "a resumed auto run must keep dispatching, not gate"
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert all(v == "DONE" for v in statuses.values()), statuses


# ---------------------------------------------------------------------------
# Reconciliation guards: no-op when there is no estimate / no agent usage
# ---------------------------------------------------------------------------

def test_result_total_tokens_none_when_usage_all_none() -> None:
    # A usage envelope present but with every token component absent (e.g. a
    # plain-text custom agent that still attached an empty usage dict) reads as
    # None — reconciliation is skipped, not compared against a misleading zero.
    result = AgentResult(agent_type="build", data={}, raw="", usage={})
    assert _result_total_tokens(result) is None


def test_result_total_tokens_sums_present_components() -> None:
    result = AgentResult(
        agent_type="build", data={}, raw="",
        usage={"input_tokens": 10, "output_tokens": 5,
               "cache_read_input_tokens": None, "cache_creation_input_tokens": 2},
    )
    assert _result_total_tokens(result) == 17


def test_reconcile_estimate_noop_without_estimate() -> None:
    # No estimate → return immediately, never touching the ledger (so passing
    # None for the ledger is safe and proves the early-out is taken).
    _reconcile_estimate(None, "run-x", "s1-001", "build", None, None)


# ---------------------------------------------------------------------------
# Cost-gate close-out renders the run view when one is wired (build + resume)
# ---------------------------------------------------------------------------

def test_cost_gate_close_out_renders_view(tmp_path: Path) -> None:
    # When a render_view is supplied, the interactive cost-gate close-out renders
    # the paused run so the terminal reflects the IN_PROGRESS state at the halt.
    from sdlc.discovery import discover_queue

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-66-sample.md").write_text(_GATE_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-66", tmp_path)
    opts = BuildOptions(
        scope="epic-66", skip_preflight=True, sequential=True, auto=False,
        cost_estimate_threshold=1,
    )
    seen: list[str] = []
    result = run_build(
        opts, queue=queue, ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        render_view=seen.append,
    )
    assert result.cost_gated is True
    assert result.run_id in seen


def test_resume_cost_gate_renders_view(tmp_path: Path) -> None:
    # The resume cost-gate close-out renders the run view too when re-gated.
    from sdlc.resume import run_resume

    db, result = _build_cost_gated(tmp_path)
    seen: list[str] = []
    resumed = run_resume(
        "epic-66", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        render_view=seen.append,
    )
    assert resumed.cost_gated is True
    assert result.run_id in seen


# ---------------------------------------------------------------------------
# CLI surfacing: cost-gate report + --cost-threshold parse error
# ---------------------------------------------------------------------------

def test_cli_build_reports_cost_gate(tmp_path: Path, monkeypatch) -> None:
    # A cost-gated build is not "clean": the CLI reports the pause and exits 1 so
    # a wrapping script knows to raise --cost-threshold and resume.
    from typer.testing import CliRunner

    import sdlc.build as build_mod
    from sdlc.build import BuildResult
    from sdlc.cli import app

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-66-sample.md").write_text(_GATE_EPIC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def _fake_run_build(opts, **kwargs):
        return BuildResult(completed=0, run_id="run-x", cost_gated=True)

    monkeypatch.setattr(build_mod, "run_build", _fake_run_build)
    result = CliRunner().invoke(app, ["build", "epic-66", "--cost-threshold=1"])
    assert result.exit_code == 1, result.output
    assert "cost gate reached" in result.output


def test_cli_resume_invalid_cost_threshold_errors(tmp_path: Path, monkeypatch) -> None:
    # A malformed --cost-threshold is rejected with exit 2 before any resume work.
    from typer.testing import CliRunner

    from sdlc.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["resume", "epic-66", "--cost-threshold=-1"])
    assert result.exit_code == 2, result.output
    assert "error:" in result.output


def test_cli_resume_reports_cost_gate(tmp_path: Path, monkeypatch) -> None:
    # An un-raised resume re-trips the gate; the CLI reports it and exits 1.
    from typer.testing import CliRunner

    import sdlc.resume as resume_mod
    from sdlc.cli import app
    from sdlc.resume import ResumeResult

    monkeypatch.chdir(tmp_path)

    def _fake_run_resume(scope, **kwargs):
        return ResumeResult(run_id="run-x", cost_gated=True)

    monkeypatch.setattr(resume_mod, "run_resume", _fake_run_resume)
    result = CliRunner().invoke(app, ["resume", "epic-66"])
    assert result.exit_code == 1, result.output
    assert "cost gate still in effect" in result.output
