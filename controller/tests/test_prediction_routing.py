# ABOUTME: Tests prediction-keyed model escalation (Story 28.3-001) — build/review
# ABOUTME: escalate on predicted tokens / rework risk, points survive only as fallback.

from __future__ import annotations

import inspect
import re

import sdlc.build as build_mod
from sdlc.build import BuildOptions, Ledger, _predict_story_cost, _select_stage_model
from sdlc.cohort import Story
from sdlc.model_routing import (
    BALANCED,
    HAIKU,
    OPUS,
    QUOTA_MAX,
    SONNET,
    EscalationSignal,
    config_from_snapshot,
    load_routing_config,
    routing_snapshot,
    select_model,
)
from sdlc.predictor import StoryPrediction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A confident signal comfortably above both Balanced thresholds.
_HOT = EscalationSignal(
    predicted_tokens=BALANCED.predicted_tokens_threshold,
    rework_probability=BALANCED.rework_threshold,
    low_confidence=False,
)
# A confident signal comfortably below both Balanced thresholds.
_COOL = EscalationSignal(
    predicted_tokens=BALANCED.predicted_tokens_threshold // 10,
    rework_probability=0.05,
    low_confidence=False,
)


def _story(points: int = 1, sid: str = "28.3-001") -> Story:
    return Story(
        id=sid, title="t", epic_id="epic-28", epic_name="e",
        epic_file="f.md", priority="Should", points=points, agent_type="python",
    )


def _prediction(
    tokens: int, rework: float, *, low_confidence: bool = False
) -> StoryPrediction:
    return StoryPrediction(
        predicted_tokens=tokens,
        predicted_rework_probability=rework,
        low_confidence=low_confidence,
        version="v1",
        cohort_key="global",
        cohort_tier="global",
        sample_size=20,
        basis="test",
    )


# ---------------------------------------------------------------------------
# select_model: prediction-keyed escalation (AC1)
# ---------------------------------------------------------------------------


def test_build_escalates_on_predicted_tokens_at_threshold() -> None:
    signal = EscalationSignal(
        predicted_tokens=BALANCED.predicted_tokens_threshold, rework_probability=0.05,
    )
    assert select_model("build", BALANCED, signal=signal) == OPUS


def test_review_escalates_on_predicted_rework_at_threshold() -> None:
    signal = EscalationSignal(
        predicted_tokens=1_000, rework_probability=BALANCED.rework_threshold,
    )
    assert select_model("review", BALANCED, signal=signal) == OPUS


def test_confident_cheap_prediction_stays_on_base_tier() -> None:
    assert select_model("build", BALANCED, signal=_COOL) == SONNET
    assert select_model("review", BALANCED, signal=_COOL) == SONNET


def test_confident_prediction_ignores_fallback_points_entirely() -> None:
    """AC4: with a confident prediction, points can neither escalate nor
    de-escalate — the noisy label is metadata, not an escalation input."""
    huge = BALANCED.points_threshold + 100
    assert select_model("build", BALANCED, fallback_points=huge, signal=_COOL) == SONNET
    assert select_model("build", BALANCED, fallback_points=0, signal=_HOT) == OPUS


def test_partial_confident_signal_is_still_prediction_keyed() -> None:
    """A confident signal carrying only one of the two figures keys on that one."""
    tokens_only = EscalationSignal(
        predicted_tokens=BALANCED.predicted_tokens_threshold, rework_probability=None,
    )
    rework_only = EscalationSignal(
        predicted_tokens=None, rework_probability=BALANCED.rework_threshold,
    )
    assert select_model("build", BALANCED, fallback_points=99, signal=tokens_only) == OPUS
    assert select_model("build", BALANCED, fallback_points=99, signal=rework_only) == OPUS


def test_empty_signal_falls_back_to_points() -> None:
    """A signal with neither figure is unusable — Epic-14 fallback applies."""
    empty = EscalationSignal()
    assert not empty.confident
    at = BALANCED.points_threshold
    assert select_model("build", BALANCED, fallback_points=at, signal=empty) == OPUS
    assert select_model("build", BALANCED, fallback_points=1, signal=empty) == SONNET


# ---------------------------------------------------------------------------
# Epic-08 risk input unchanged (AC2)
# ---------------------------------------------------------------------------


def test_high_risk_escalates_even_when_prediction_says_cheap() -> None:
    """The risk input is preserved: this story replaces points, not risk_gate."""
    assert select_model("review", BALANCED, high_risk=True, signal=_COOL) == OPUS
    assert select_model("build", BALANCED, high_risk=True, signal=_COOL) == OPUS


def test_adversarial_opus_floor_holds_on_high_risk_with_prediction() -> None:
    assert select_model("adversarial", BALANCED, high_risk=True, signal=_COOL) == OPUS


def test_adversarial_escalates_on_hot_prediction_and_tiers_down_on_cool() -> None:
    """The 27.2-002 floor semantics carry over: a predicted-heavy story pays the
    Opus floor, a predicted-cheap low-risk one tiers the skeptic down."""
    assert select_model("adversarial", BALANCED, signal=_HOT) == OPUS
    assert select_model("adversarial", BALANCED, signal=_COOL) == SONNET


def test_non_escalatable_stages_ignore_the_prediction() -> None:
    assert select_model("coverage", BALANCED, signal=_HOT) == SONNET
    assert select_model("merge", BALANCED, signal=_HOT) == HAIKU


# ---------------------------------------------------------------------------
# Low-confidence / disabled fallback to Epic-14 behaviour (AC3)
# ---------------------------------------------------------------------------


def test_low_confidence_prediction_falls_back_to_points() -> None:
    low = EscalationSignal(
        predicted_tokens=BALANCED.predicted_tokens_threshold * 2,
        rework_probability=0.9,
        low_confidence=True,
    )
    at = BALANCED.points_threshold
    # Low-confidence never escalates on its own figures — points decide.
    assert select_model("build", BALANCED, fallback_points=1, signal=low) == SONNET
    assert select_model("build", BALANCED, fallback_points=at, signal=low) == OPUS


def test_no_signal_is_todays_points_keyed_behaviour() -> None:
    at = BALANCED.points_threshold
    assert select_model("build", BALANCED, fallback_points=at) == OPUS
    assert select_model("build", BALANCED, fallback_points=at - 1) == SONNET


def test_routing_off_returns_none_regardless_of_signal() -> None:
    assert select_model("build", None, signal=_HOT) is None


# ---------------------------------------------------------------------------
# Configurable thresholds: profile defaults + per-repo override (AC5)
# ---------------------------------------------------------------------------


def test_quota_max_has_a_higher_prediction_bar_than_balanced() -> None:
    assert QUOTA_MAX.predicted_tokens_threshold > BALANCED.predicted_tokens_threshold
    assert QUOTA_MAX.rework_threshold > BALANCED.rework_threshold
    # A story hot for Balanced is still cheap for Quota-max.
    assert select_model("build", QUOTA_MAX, signal=_HOT) == HAIKU


def test_override_can_change_predicted_tokens_threshold() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  predicted_tokens_threshold: 1000\n",
    )
    assert cfg is not None and cfg.predicted_tokens_threshold == 1000
    signal = EscalationSignal(predicted_tokens=1000, rework_probability=0.0)
    assert select_model("build", cfg, signal=signal) == OPUS


def test_override_can_change_rework_threshold() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  rework_threshold: 0.2\n",
    )
    assert cfg is not None and cfg.rework_threshold == 0.2
    signal = EscalationSignal(predicted_tokens=1, rework_probability=0.2)
    assert select_model("build", cfg, signal=signal) == OPUS


def test_invalid_threshold_overrides_raise() -> None:
    import pytest

    with pytest.raises(ValueError, match="must be an integer"):
        load_routing_config(
            "balanced",
            override_text="model_routing:\n  predicted_tokens_threshold: lots\n",
        )
    with pytest.raises(ValueError, match="must be a number"):
        load_routing_config(
            "balanced",
            override_text="model_routing:\n  rework_threshold: likely\n",
        )


def test_snapshot_round_trips_the_prediction_thresholds() -> None:
    """The 28.4-001 freeze carries the new thresholds, so a resume escalates
    identically to its original run."""
    snap = routing_snapshot(QUOTA_MAX)
    assert snap["predicted_tokens_threshold"] == QUOTA_MAX.predicted_tokens_threshold
    assert snap["rework_threshold"] == QUOTA_MAX.rework_threshold
    cfg = config_from_snapshot(snap)
    assert cfg is not None
    assert cfg.predicted_tokens_threshold == QUOTA_MAX.predicted_tokens_threshold
    assert cfg.rework_threshold == QUOTA_MAX.rework_threshold


def test_legacy_snapshot_without_thresholds_gets_balanced_defaults() -> None:
    snap = routing_snapshot(BALANCED)
    snap.pop("predicted_tokens_threshold")
    snap.pop("rework_threshold")
    cfg = config_from_snapshot(snap)
    assert cfg is not None
    assert cfg.predicted_tokens_threshold == BALANCED.predicted_tokens_threshold
    assert cfg.rework_threshold == BALANCED.rework_threshold


# ---------------------------------------------------------------------------
# Build wiring: _select_stage_model consumes the committed prediction
# ---------------------------------------------------------------------------


def test_stage_model_uses_the_cached_confident_prediction() -> None:
    opts = BuildOptions(model_profile="balanced")
    story = _story(points=1)
    opts._story_predictions[story.id] = _HOT
    assert _select_stage_model("build", story, opts) == OPUS


def test_stage_model_ignores_points_when_prediction_is_confident() -> None:
    opts = BuildOptions(model_profile="balanced")
    story = _story(points=BALANCED.points_threshold + 5)
    opts._story_predictions[story.id] = _COOL
    assert _select_stage_model("build", story, opts) == SONNET


def test_stage_model_without_prediction_keeps_points_fallback() -> None:
    opts = BuildOptions(model_profile="balanced")
    assert _select_stage_model("build", _story(points=13), opts) == OPUS
    assert _select_stage_model("build", _story(points=1), opts) == SONNET


def test_routing_path_no_longer_reads_points_as_the_escalation_input() -> None:
    """AC4 (the 'search' half, kept honest by CI): the routing path passes
    `story.points` only as the labelled Epic-14 fallback, never as `points=`."""
    source = inspect.getsource(build_mod._select_stage_model)
    assert re.search(r"(?<!fallback_)\bpoints=story\.points", source) is None
    assert "fallback_points=story.points" in source
    # And select_model itself no longer accepts a bare `points` escalation input.
    params = inspect.signature(select_model).parameters
    assert "points" not in params
    assert "fallback_points" in params


# ---------------------------------------------------------------------------
# _predict_story_cost populates (and replays) the routing cache
# ---------------------------------------------------------------------------


def _predicting_opts() -> BuildOptions:
    return BuildOptions(model_profile="balanced", predict=True)


def _seed_run_story(ledger: Ledger, story: Story) -> str:
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, story.id, story.epic_id, story.title, story.priority,
        story.points, story.agent_type, "", None, "TODO",
    )
    return run_id


def _events(ledger: Ledger, run_id: str) -> list[str]:
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        return [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]


def test_fresh_prediction_lands_in_the_routing_cache(tmp_path, monkeypatch) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    monkeypatch.setattr(
        build_mod, "predict_story",
        lambda features, history, **kw: _prediction(9_999_999, 0.9),
    )
    opts = _predicting_opts()
    _predict_story_cost(ledger, run_id, story, opts)
    signal = opts._story_predictions[story.id]
    assert signal.predicted_tokens == 9_999_999
    assert signal.rework_probability == 0.9
    assert signal.confident
    assert _select_stage_model("build", story, opts) == OPUS


def test_low_confidence_prediction_is_cached_but_not_confident(
    tmp_path, monkeypatch
) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    monkeypatch.setattr(
        build_mod, "predict_story",
        lambda features, history, **kw: _prediction(
            9_999_999, 0.9, low_confidence=True
        ),
    )
    opts = _predicting_opts()
    _predict_story_cost(ledger, run_id, story, opts)
    assert not opts._story_predictions[story.id].confident
    # Fallback: a small story stays on its cheap tier despite the hot figures.
    assert _select_stage_model("build", story, opts) == SONNET


def test_reentered_story_replays_the_committed_prediction(tmp_path) -> None:
    """Resume determinism: the committed ledger forecast — not a re-prediction —
    is what escalation keys on when a story is re-entered."""
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    ledger.story_set_prediction(
        run_id, story.id,
        predicted_tokens=BALANCED.predicted_tokens_threshold + 1,
        predicted_rework_prob=0.1,
        predictor_version="v1",
        low_confidence=False,
    )
    opts = _predicting_opts()
    _predict_story_cost(ledger, run_id, story, opts)  # re-entry: must not re-predict
    signal = opts._story_predictions[story.id]
    assert signal.predicted_tokens == BALANCED.predicted_tokens_threshold + 1
    assert signal.confident
    assert _select_stage_model("build", story, opts) == OPUS


def test_predictor_off_leaves_the_cache_empty(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    opts = BuildOptions(model_profile="balanced", predict=False)
    _predict_story_cost(ledger, run_id, story, opts)
    assert opts._story_predictions == {}


def test_ledger_story_prediction_accessor(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    assert ledger.story_prediction(run_id, story.id) is None
    ledger.story_set_prediction(
        run_id, story.id,
        predicted_tokens=123, predicted_rework_prob=0.25,
        predictor_version="v1", low_confidence=True,
    )
    row = ledger.story_prediction(run_id, story.id)
    assert row == {
        "predicted_tokens": 123,
        "predicted_rework_prob": 0.25,
        "prediction_confidence": "low",
    }


def test_low_confidence_fallback_is_logged(tmp_path, monkeypatch) -> None:
    """AC3: the fallback to points-keyed escalation is stated, not silent."""
    ledger = Ledger(tmp_path / "ledger.db")
    story = _story()
    run_id = _seed_run_story(ledger, story)
    monkeypatch.setattr(
        build_mod, "predict_story",
        lambda features, history, **kw: _prediction(1, 0.1, low_confidence=True),
    )
    _predict_story_cost(ledger, run_id, story, _predicting_opts())
    assert any("points-keyed" in m for m in _events(ledger, run_id))
