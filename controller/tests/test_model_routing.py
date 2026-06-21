# ABOUTME: Tests for per-task model routing (Story 14.2-001) — the Balanced default map.
# ABOUTME: Covers map selection, risk/points escalation, the adversarial Opus pin, and overrides.

from __future__ import annotations

import textwrap

import pytest

from sdlc.model_routing import (
    BALANCED,
    HAIKU,
    OPUS,
    QUALITY_FIRST,
    QUOTA_MAX,
    SONNET,
    TIER_LADDER,
    escalate_model,
    load_routing_config,
    routing_config,
    select_model,
)

# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def test_routing_off_when_profile_blank_or_none() -> None:
    """An unset / off profile disables routing (returns None → CLI default)."""
    for name in ("", "off", "none", None):
        assert routing_config(name) is None


def test_routing_off_is_case_insensitive() -> None:
    assert routing_config("OFF") is None
    assert routing_config("None") is None


def test_balanced_is_the_default_profile() -> None:
    cfg = routing_config("balanced")
    assert cfg is not None
    assert cfg.profile == "balanced"
    assert cfg is BALANCED


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="unknown model-routing profile"):
        routing_config("turbo")


# ---------------------------------------------------------------------------
# The Balanced default map (the acceptance-criteria table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage, expected",
    [
        ("discovery", HAIKU),
        ("build", SONNET),
        ("coverage", SONNET),
        ("review", SONNET),
        ("adversarial", OPUS),
        ("merge", HAIKU),
    ],
)
def test_balanced_default_map(stage, expected) -> None:
    """The Balanced default matches the story's per-stage table for a small, low-risk story."""
    assert select_model(stage, BALANCED, points=1, high_risk=False) == expected


def test_unknown_stage_returns_none() -> None:
    """A stage not in the map adds no --model (graceful: CLI default)."""
    assert select_model("nonexistent", BALANCED, points=1, high_risk=False) is None


# ---------------------------------------------------------------------------
# Escalation: build / review → Opus on high-risk or large story
# ---------------------------------------------------------------------------


def test_build_escalates_to_opus_when_high_risk() -> None:
    assert select_model("build", BALANCED, points=1, high_risk=True) == OPUS


def test_review_escalates_to_opus_when_high_risk() -> None:
    assert select_model("review", BALANCED, points=1, high_risk=True) == OPUS


def test_build_escalates_to_opus_when_points_at_or_above_threshold() -> None:
    assert BALANCED.points_threshold > 0
    at = BALANCED.points_threshold
    assert select_model("build", BALANCED, points=at, high_risk=False) == OPUS
    assert select_model("build", BALANCED, points=at + 1, high_risk=False) == OPUS


def test_build_stays_sonnet_below_threshold_and_low_risk() -> None:
    below = BALANCED.points_threshold - 1
    assert select_model("build", BALANCED, points=below, high_risk=False) == SONNET


def test_non_escalatable_stages_do_not_escalate() -> None:
    """coverage / merge / discovery never escalate on risk or points."""
    big = BALANCED.points_threshold + 5
    assert select_model("coverage", BALANCED, points=big, high_risk=True) == SONNET
    assert select_model("merge", BALANCED, points=big, high_risk=True) == HAIKU
    assert select_model("discovery", BALANCED, points=big, high_risk=True) == HAIKU


# ---------------------------------------------------------------------------
# The adversarial pin — always Opus, never downgraded, in every profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cfg", [BALANCED, QUALITY_FIRST, QUOTA_MAX])
def test_adversarial_is_always_opus(cfg) -> None:
    assert select_model("adversarial", cfg, points=1, high_risk=False) == OPUS
    # Even with a low points / no risk, even in the cheapest profile.
    assert select_model("adversarial", cfg, points=0, high_risk=False) == OPUS


def test_adversarial_pin_survives_a_cheap_override() -> None:
    """A per-repo override cannot cheapen the pinned adversarial slot."""
    cfg = load_routing_config(
        "quota-max",
        override_text="model_routing:\n  stages:\n    adversarial: haiku\n",
    )
    assert select_model("adversarial", cfg, points=1, high_risk=False) == OPUS


# ---------------------------------------------------------------------------
# The alternative documented profiles
# ---------------------------------------------------------------------------


def test_quality_first_is_opus_everywhere() -> None:
    for stage in ("discovery", "build", "coverage", "review", "merge", "adversarial"):
        assert select_model(stage, QUALITY_FIRST, points=1, high_risk=False) == OPUS


def test_quota_max_is_cheap_but_pins_adversarial() -> None:
    assert select_model("build", QUOTA_MAX, points=1, high_risk=False) == HAIKU
    assert select_model("merge", QUOTA_MAX, points=1, high_risk=False) == HAIKU
    assert select_model("adversarial", QUOTA_MAX, points=1, high_risk=False) == OPUS
    # Quota-max still protects genuinely high-stakes build/review work.
    assert select_model("build", QUOTA_MAX, points=1, high_risk=True) == OPUS


# ---------------------------------------------------------------------------
# Per-repo override loading
# ---------------------------------------------------------------------------


def test_load_routing_config_no_override_returns_base_profile() -> None:
    assert load_routing_config("balanced") is BALANCED


def test_override_remaps_a_stage_model() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  stages:\n    build: opus\n",
    )
    assert select_model("build", cfg, points=1, high_risk=False) == OPUS
    # Other stages keep the base profile's value.
    assert select_model("coverage", cfg, points=1, high_risk=False) == SONNET


def test_override_can_change_points_threshold() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  points_threshold: 3\n",
    )
    assert cfg.points_threshold == 3
    assert select_model("build", cfg, points=3, high_risk=False) == OPUS


def test_override_can_select_base_profile() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  profile: quota-max\n",
    )
    assert select_model("build", cfg, points=1, high_risk=False) == HAIKU


def test_override_reads_from_file(tmp_path) -> None:
    path = tmp_path / ".sdlc-model-routing.yaml"
    path.write_text(
        textwrap.dedent(
            """
            model_routing:
              stages:
                merge: sonnet
            """
        ),
        encoding="utf-8",
    )
    cfg = load_routing_config("balanced", override_path=path)
    assert select_model("merge", cfg, points=1, high_risk=False) == SONNET


def test_missing_override_file_is_ignored(tmp_path) -> None:
    cfg = load_routing_config("balanced", override_path=tmp_path / "absent.yaml")
    assert cfg is BALANCED


def test_load_routing_config_off_returns_none() -> None:
    assert load_routing_config("off") is None
    assert load_routing_config("") is None


def test_malformed_override_raises() -> None:
    with pytest.raises(ValueError):
        load_routing_config(
            "balanced", override_text="model_routing:\n  stages: not-a-mapping\n"
        )


@pytest.mark.parametrize(
    "text, match",
    [
        ("nope: true\n", "must define a top-level"),
        ("model_routing: not-a-mapping\n", "must be a mapping"),
        ("model_routing:\n  profile: off\n", "cannot be 'off'"),
        ("model_routing:\n  points_threshold: nine\n", "must be an integer"),
        ("model_routing:\n  points_threshold: true\n", "must be an integer"),
        ("model_routing:\n  escalation_model: [opus]\n", "must be a string"),
    ],
)
def test_invalid_override_shapes_raise(text, match) -> None:
    with pytest.raises(ValueError, match=match):
        load_routing_config("balanced", override_text=text)


def test_invalid_override_yaml_raises() -> None:
    with pytest.raises(ValueError, match="not valid YAML"):
        load_routing_config("balanced", override_text="model_routing:\n  : :\n  ]")


def test_override_can_change_escalation_model() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  escalation_model: claude-opus-4-8\n",
    )
    assert select_model("adversarial", cfg, points=1, high_risk=False) == "claude-opus-4-8"
    assert select_model("build", cfg, points=99, high_risk=True) == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Config immutability / shape
# ---------------------------------------------------------------------------


def test_config_is_frozen() -> None:
    with pytest.raises(Exception):
        BALANCED.points_threshold = 99  # type: ignore[misc]


def test_select_model_with_none_config_is_routing_off() -> None:
    assert select_model("build", None, points=99, high_risk=True) is None


# ---------------------------------------------------------------------------
# escalate_model — cheap-first retry tier bump (Story 14.2-003)
# ---------------------------------------------------------------------------


def test_tier_ladder_is_cheap_to_strong() -> None:
    assert TIER_LADDER == (HAIKU, SONNET, OPUS)


def test_escalate_climbs_one_tier_per_step() -> None:
    assert escalate_model(HAIKU, 1) == SONNET
    assert escalate_model(HAIKU, 2) == OPUS
    assert escalate_model(SONNET, 1) == OPUS


def test_escalate_caps_at_top_tier() -> None:
    # Beyond the strongest tier is a no-op — never overshoots Opus (AC1 cap).
    assert escalate_model(HAIKU, 3) == OPUS
    assert escalate_model(SONNET, 5) == OPUS


def test_escalate_top_tier_is_a_no_op() -> None:
    # AC3: escalating an already-Opus stage does nothing.
    assert escalate_model(OPUS, 1) == OPUS
    assert escalate_model(OPUS, 9) == OPUS


def test_escalate_zero_or_negative_steps_is_unchanged() -> None:
    # The cheap first-pass path passes steps=0 → no escalation (AC2).
    assert escalate_model(SONNET, 0) == SONNET
    assert escalate_model(HAIKU, -1) == HAIKU


def test_escalate_none_base_stays_none() -> None:
    # Routing off → no base model → escalation keeps the CLI default.
    assert escalate_model(None, 1) is None
    assert escalate_model(None, 0) is None


def test_escalate_unknown_model_is_left_untouched() -> None:
    # A custom / pinned model id the ladder cannot reason about is never
    # silently rewritten — it is returned verbatim.
    assert escalate_model("claude-opus-4-8", 1) == "claude-opus-4-8"
    assert escalate_model("some-vendor-model", 2) == "some-vendor-model"
