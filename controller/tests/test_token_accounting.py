# ABOUTME: Tests for honest token accounting across harnesses (Story 31.2-002).
# ABOUTME: Component breakdown, capability-gated availability, estimate labelling, the counter seam.

from __future__ import annotations

import pytest

from sdlc.usage import (
    ESTIMATED,
    EXTERNAL,
    MEASURED,
    MIXED,
    UNAVAILABLE,
    UNAVAILABLE_USAGE,
    TokenBreakdown,
    aggregate_source,
    breakdown_from_envelope,
    describe_mix,
    harness_breakdown,
    mix_divergence,
    register_token_counter,
    unregister_token_counter,
)

# The field finding this story exists for: two arms, totals within 7% of each
# other, describing completely different work at completely different prices.
CACHE_WRITE_ARM = TokenBreakdown(
    input_tokens=8, output_tokens=7, cache_read_tokens=0,
    cache_creation_tokens=15145, source=MEASURED,
)
CACHE_READ_ARM = TokenBreakdown(
    input_tokens=6, output_tokens=9, cache_read_tokens=14137,
    cache_creation_tokens=0, source=MEASURED,
)


# ---------------------------------------------------------------------------
# TokenBreakdown — components, total, mix
# ---------------------------------------------------------------------------


def test_unavailable_breakdown_has_no_total_and_is_not_zero() -> None:
    assert UNAVAILABLE_USAGE.total is None
    assert UNAVAILABLE_USAGE.available is False
    assert UNAVAILABLE_USAGE.source == UNAVAILABLE
    assert UNAVAILABLE_USAGE.mix is None


def test_total_sums_the_four_components_treating_absent_as_zero() -> None:
    assert CACHE_WRITE_ARM.total == 15160
    assert CACHE_READ_ARM.total == 14152


def test_mix_is_the_per_component_share_of_the_total() -> None:
    mix = CACHE_WRITE_ARM.mix
    assert mix is not None
    assert mix["cache_creation"] == pytest.approx(0.999, abs=1e-3)
    assert mix["cache_read"] == pytest.approx(0.0, abs=1e-3)
    assert sum(mix.values()) == pytest.approx(1.0)


def test_near_equal_totals_can_have_maximally_divergent_mixes() -> None:
    # 15,160 vs 14,152 — within 7% on the total, ~100 points apart on the mix.
    totals_gap = abs(CACHE_WRITE_ARM.total - CACHE_READ_ARM.total) / CACHE_WRITE_ARM.total  # type: ignore[operator]
    assert totals_gap < 0.07
    assert mix_divergence(CACHE_WRITE_ARM.mix, CACHE_READ_ARM.mix) > 0.9  # type: ignore[arg-type]


def test_describe_mix_names_the_dominant_component() -> None:
    assert describe_mix(CACHE_WRITE_ARM.mix) == "99.9% cache_creation"
    assert describe_mix(CACHE_READ_ARM.mix) == "99.9% cache_read"
    assert describe_mix(None) == "unknown mix"


# ---------------------------------------------------------------------------
# breakdown_from_envelope
# ---------------------------------------------------------------------------


def test_envelope_maps_the_four_agent_usage_keys() -> None:
    b = breakdown_from_envelope(
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        }
    )
    assert (b.input_tokens, b.output_tokens) == (1, 2)
    assert (b.cache_read_tokens, b.cache_creation_tokens) == (3, 4)
    assert b.source == MEASURED
    assert b.total == 10


def test_empty_or_all_none_envelope_is_unavailable_not_zero() -> None:
    assert breakdown_from_envelope(None) is UNAVAILABLE_USAGE
    assert breakdown_from_envelope({}) is UNAVAILABLE_USAGE
    assert breakdown_from_envelope({"input_tokens": None}) is UNAVAILABLE_USAGE


def test_a_genuine_zero_component_is_kept_as_zero() -> None:
    b = breakdown_from_envelope({"input_tokens": 0, "output_tokens": 5})
    assert b.input_tokens == 0
    assert b.total == 5
    assert b.available is True


# ---------------------------------------------------------------------------
# Capability-driven availability (AC3 / AC5)
# ---------------------------------------------------------------------------

_FULL_ENVELOPE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 40,
}


def test_harness_without_usage_tracking_reports_unavailable_never_zero() -> None:
    # A harness that declares usage_tracking: false but prints numbers anyway
    # earns no token figure: undeclared/false capability means absent.
    b = harness_breakdown(_FULL_ENVELOPE, capabilities={"usage_tracking": False})
    assert b.total is None
    assert b.total != 0
    assert b.source == UNAVAILABLE


def test_undeclared_usage_tracking_is_absent() -> None:
    assert harness_breakdown(_FULL_ENVELOPE, capabilities={}).total is None


def test_flipping_the_capability_makes_the_axis_available_with_no_other_change() -> None:
    # Epic-29 29.2-003: OpenCode grows usage telemetry -> flip the flag, done.
    before = harness_breakdown(_FULL_ENVELOPE, capabilities={"usage_tracking": False})
    after = harness_breakdown(_FULL_ENVELOPE, capabilities={"usage_tracking": True})
    assert before.source == UNAVAILABLE
    assert after.source == MEASURED
    assert after.total == 100


def test_no_capability_map_keeps_todays_behaviour() -> None:
    assert harness_breakdown(_FULL_ENVELOPE).total == 100


# ---------------------------------------------------------------------------
# The external token-count seam (AC7)
# ---------------------------------------------------------------------------


def test_registered_counter_supplies_an_approximate_breakdown() -> None:
    def counter(_usage: object) -> TokenBreakdown:
        return TokenBreakdown(input_tokens=900, output_tokens=100)

    register_token_counter("local-llm", counter)
    try:
        b = harness_breakdown(
            None, capabilities={"usage_tracking": False}, harness="local-llm"
        )
    finally:
        unregister_token_counter("local-llm")
    assert b.total == 1000
    assert b.source == EXTERNAL
    assert b.approximate is True


def test_counter_is_only_consulted_for_its_own_harness() -> None:
    register_token_counter("local-llm", lambda _u: TokenBreakdown(input_tokens=1))
    try:
        other = harness_breakdown(
            None, capabilities={"usage_tracking": False}, harness="opencode"
        )
    finally:
        unregister_token_counter("local-llm")
    assert other is UNAVAILABLE_USAGE


def test_a_counter_that_declines_leaves_the_axis_unavailable() -> None:
    register_token_counter("local-llm", lambda _u: None)
    try:
        b = harness_breakdown(
            None, capabilities={"usage_tracking": False}, harness="local-llm"
        )
    finally:
        unregister_token_counter("local-llm")
    assert b is UNAVAILABLE_USAGE


# ---------------------------------------------------------------------------
# Provenance (AC6)
# ---------------------------------------------------------------------------


def test_estimates_are_approximate_measurements_are_not() -> None:
    est = breakdown_from_envelope(_FULL_ENVELOPE, source=ESTIMATED)
    assert est.approximate is True
    assert breakdown_from_envelope(_FULL_ENVELOPE).approximate is False


def test_aggregate_source_folds_run_provenance() -> None:
    measured = breakdown_from_envelope(_FULL_ENVELOPE)
    estimated = breakdown_from_envelope(_FULL_ENVELOPE, source=ESTIMATED)
    assert aggregate_source([]) == UNAVAILABLE
    assert aggregate_source([UNAVAILABLE_USAGE]) == UNAVAILABLE
    assert aggregate_source([measured, measured]) == MEASURED
    assert aggregate_source([measured, UNAVAILABLE_USAGE]) == MEASURED
    assert aggregate_source([estimated, estimated]) == ESTIMATED
    assert aggregate_source([measured, estimated]) == MIXED
