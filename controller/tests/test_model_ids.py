# ABOUTME: Story 34.1-001 — tier alias → exact model id map, with alias/full-id compatibility.
# ABOUTME: Covers resolution, override passthrough, escalation by tier, banner, and the probe.
from __future__ import annotations

from sdlc.build import BuildOptions, _resolved_stage_model
from sdlc.harness import CLAUDE_RATE_LIMIT_PROBE, DEFAULT_HARNESS, load_harnesses_config
from sdlc.model_routing import (
    BALANCED,
    HAIKU,
    OPUS,
    SONNET,
    TIER_LADDER,
    TIER_MODEL_IDS,
    escalate_model,
    resolve_model_id,
    routing_banner,
    routing_snapshot,
)


from test_harness import CONFIG_PATH  # noqa: E402


def _story():
    from test_build import _sample_queue

    return _sample_queue()[0]


def test_every_ladder_tier_has_a_model_id() -> None:
    assert set(TIER_MODEL_IDS) == set(TIER_LADDER)
    assert TIER_MODEL_IDS == {
        HAIKU: "claude-haiku-4-5",
        SONNET: "claude-sonnet-5",
        OPUS: "claude-opus-5-5",
    }


def test_resolve_model_id_maps_tiers_and_passes_everything_else() -> None:
    assert resolve_model_id("opus") == "claude-opus-5-5"
    assert resolve_model_id("claude-opus-4-8") == "claude-opus-4-8"  # pinned id
    assert resolve_model_id("gpt-5") == "gpt-5"  # registry harness model
    assert resolve_model_id(None) is None  # routing off


def test_balanced_build_dispatches_the_sonnet_id() -> None:
    opts = BuildOptions(scope="epic-34", skip_preflight=True, sequential=True)
    assert _resolved_stage_model("build", _story(), opts) == "claude-sonnet-5"


def test_alias_override_resolves_through_the_table() -> None:
    opts = BuildOptions(
        scope="epic-34", skip_preflight=True, sequential=True,
        model_overrides={"build": "opus"},
    )
    assert _resolved_stage_model("build", _story(), opts) == "claude-opus-5-5"


def test_full_id_override_passes_through_untouched() -> None:
    opts = BuildOptions(
        scope="epic-34", skip_preflight=True, sequential=True,
        model_overrides={"build": "claude-opus-4-8"},
    )
    assert _resolved_stage_model("build", _story(), opts) == "claude-opus-4-8"


def test_escalation_climbs_by_tier_then_dispatches_the_mapped_id() -> None:
    climbed = [escalate_model(HAIKU, n) for n in range(3)]
    assert climbed == [HAIKU, SONNET, OPUS]
    assert [resolve_model_id(t) for t in climbed] == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5-5",
    ]


def test_routing_banner_prints_alias_and_id() -> None:
    snap = routing_snapshot(BALANCED, overrides={"build": "opus"})
    banner = "\n".join(routing_banner(snap))
    assert "build=opus → claude-opus-5-5" in banner
    assert "merge=haiku → claude-haiku-4-5" in banner


def test_banner_leaves_a_pinned_full_id_bare() -> None:
    snap = routing_snapshot(BALANCED, overrides={"build": "claude-opus-4-8"})
    banner = "\n".join(routing_banner(snap))
    assert "build=claude-opus-4-8" in banner
    assert "claude-opus-4-8 →" not in banner


def test_rate_limit_probe_reads_the_table() -> None:
    expected = f"claude -p ok --model {TIER_MODEL_IDS[HAIKU]}"
    assert CLAUDE_RATE_LIMIT_PROBE == expected
    assert load_harnesses_config(CONFIG_PATH)[DEFAULT_HARNESS].rate_limit_probe == expected
