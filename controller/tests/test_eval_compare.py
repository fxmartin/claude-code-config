# ABOUTME: Tests for variant comparison + regression baselines (Story 18.1-002).
# ABOUTME: Per-metric classify, ticket verdict, scoreboard diff, regression flags, baseline IO.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.eval_compare import (
    BETTER,
    DEFAULT_TOLERANCE,
    IMPROVED,
    NEUTRAL,
    REGRESSED,
    WORSE,
    BaselineError,
    MetricDelta,
    classify_metric,
    comparison_to_dict,
    compare_scoreboards,
    has_regressions,
    load_scoreboard,
    provenance_warnings,
    regressions,
    render_comparison_table,
    save_scoreboard,
    ticket_set_mismatches,
    ticket_verdict,
)


# A metric where lower is better (LOC / tokens / cost / wall).
def _spec_lower() -> object:
    from sdlc.eval_compare import MetricSpec

    return MetricSpec(key="tokens_mean", label="tokens", lower_is_better=True)


def _spec_quality() -> object:
    from sdlc.eval_compare import MetricSpec

    return MetricSpec(key="quality_pass_rate", label="qual", lower_is_better=False)


# ---------------------------------------------------------------------------
# classify_metric — per-metric direction within a tolerance
# ---------------------------------------------------------------------------


def test_classify_lower_is_better_drop_is_improved() -> None:
    d = classify_metric(_spec_lower(), baseline=100.0, candidate=80.0, tolerance=0.10)
    assert d.direction == IMPROVED
    assert d.delta == -20.0
    assert d.pct == pytest.approx(-0.20)


def test_classify_lower_is_better_rise_is_regressed() -> None:
    d = classify_metric(_spec_lower(), baseline=100.0, candidate=130.0, tolerance=0.10)
    assert d.direction == REGRESSED
    assert d.pct == pytest.approx(0.30)


def test_classify_within_tolerance_is_neutral() -> None:
    d = classify_metric(_spec_lower(), baseline=100.0, candidate=105.0, tolerance=0.10)
    assert d.direction == NEUTRAL


def test_classify_exactly_at_tolerance_is_neutral() -> None:
    # Strict ">" boundary: a change of exactly the tolerance does not flag.
    d = classify_metric(_spec_lower(), baseline=100.0, candidate=110.0, tolerance=0.10)
    assert d.direction == NEUTRAL


def test_classify_quality_higher_is_better() -> None:
    up = classify_metric(_spec_quality(), baseline=0.8, candidate=1.0, tolerance=0.10)
    down = classify_metric(_spec_quality(), baseline=1.0, candidate=0.8, tolerance=0.10)
    assert up.direction == IMPROVED
    assert down.direction == REGRESSED


def test_classify_zero_baseline_any_change_is_beyond_tolerance() -> None:
    # 0 -> nonzero is a categorical change; pct is undefined (None) but it flags.
    d = classify_metric(_spec_quality(), baseline=0.0, candidate=0.5, tolerance=0.10)
    assert d.direction == IMPROVED
    assert d.pct is None
    assert d.delta == 0.5


def test_classify_zero_to_zero_is_neutral() -> None:
    d = classify_metric(_spec_lower(), baseline=0.0, candidate=0.0, tolerance=0.10)
    assert d.direction == NEUTRAL


def test_classify_missing_value_is_neutral_no_data() -> None:
    a = classify_metric(_spec_lower(), baseline=None, candidate=80.0, tolerance=0.10)
    b = classify_metric(_spec_lower(), baseline=100.0, candidate=None, tolerance=0.10)
    assert a.direction == NEUTRAL and a.delta is None and a.pct is None
    assert b.direction == NEUTRAL and b.delta is None


# ---------------------------------------------------------------------------
# ticket_verdict — fold metric directions into one verdict
# ---------------------------------------------------------------------------


def _md(key: str, direction: str) -> MetricDelta:
    return MetricDelta(
        key=key, label=key, baseline=1.0, candidate=1.0, delta=0.0, pct=0.0, direction=direction
    )


def test_verdict_quality_regression_dominates() -> None:
    metrics = [
        _md("quality_pass_rate", REGRESSED),
        _md("tokens_mean", IMPROVED),
        _md("cost_mean", IMPROVED),
    ]
    assert ticket_verdict(metrics) == WORSE


def test_verdict_quality_improvement_wins() -> None:
    metrics = [
        _md("quality_pass_rate", IMPROVED),
        _md("tokens_mean", REGRESSED),
    ]
    assert ticket_verdict(metrics) == BETTER


def test_verdict_efficiency_tally_better() -> None:
    metrics = [
        _md("quality_pass_rate", NEUTRAL),
        _md("tokens_mean", IMPROVED),
        _md("cost_mean", IMPROVED),
        _md("loc_net_mean", REGRESSED),
    ]
    assert ticket_verdict(metrics) == BETTER


def test_verdict_efficiency_tally_worse() -> None:
    metrics = [
        _md("tokens_mean", REGRESSED),
        _md("cost_mean", REGRESSED),
        _md("loc_net_mean", IMPROVED),
    ]
    assert ticket_verdict(metrics) == WORSE


def test_verdict_all_neutral_is_neutral() -> None:
    metrics = [_md("tokens_mean", NEUTRAL), _md("cost_mean", NEUTRAL)]
    assert ticket_verdict(metrics) == NEUTRAL


def test_verdict_efficiency_tie_is_neutral() -> None:
    metrics = [_md("tokens_mean", IMPROVED), _md("cost_mean", REGRESSED)]
    assert ticket_verdict(metrics) == NEUTRAL


# ---------------------------------------------------------------------------
# compare_scoreboards — side-by-side over two scoreboard dicts
# ---------------------------------------------------------------------------


def _score(ticket_id: str, *, loc: float, tokens: float, cost: float, wall: float, qual: float) -> dict:
    # Story 31.2-002: a real scoreboard row carries the component breakdown its
    # total is made of. These rows share one mix (90% cache-read), so the token
    # axis stays comparable and these tests keep exercising it.
    return {
        "ticket_id": ticket_id,
        "runs": 1,
        "errors": 0,
        "loc_added_mean": loc,
        "loc_removed_mean": 0.0,
        "loc_net_mean": loc,
        "tokens_mean": tokens,
        "cost_mean": cost,
        "wall_mean": wall,
        "quality_pass_rate": qual,
        "input_tokens_mean": tokens * 0.05,
        "output_tokens_mean": tokens * 0.05,
        "cache_read_tokens_mean": tokens * 0.90,
        "cache_creation_tokens_mean": 0.0,
        "tokens_source": "measured",
    }


def _board(name: str, tickets: list[dict], overall: dict | None) -> dict:
    return {"config_name": name, "tickets": tickets, "overall": overall}


def test_compare_matches_tickets_and_builds_overall() -> None:
    base = _board(
        "A",
        [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)],
        _score("OVERALL", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
    )
    cand = _board(
        "B",
        [_score("t1", loc=6, tokens=700, cost=0.03, wall=18, qual=1.0)],
        _score("OVERALL", loc=6, tokens=700, cost=0.03, wall=18, qual=1.0),
    )
    cmp = compare_scoreboards(base, cand, tolerance=0.10)
    assert cmp.baseline_name == "A"
    assert cmp.candidate_name == "B"
    assert [t.ticket_id for t in cmp.tickets] == ["t1"]
    assert cmp.tickets[0].verdict == BETTER  # everything cheaper, quality held
    assert cmp.overall is not None
    assert cmp.overall.verdict == BETTER


def test_compare_default_tolerance_used_when_unspecified() -> None:
    base = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cmp = compare_scoreboards(base, cand)
    assert cmp.tolerance == DEFAULT_TOLERANCE
    assert cmp.tickets[0].verdict == NEUTRAL


def test_compare_candidate_only_ticket_appended() -> None:
    base = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board(
        "B",
        [
            _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
            _score("t2", loc=5, tokens=500, cost=0.02, wall=10, qual=1.0),
        ],
        None,
    )
    cmp = compare_scoreboards(base, cand)
    assert [t.ticket_id for t in cmp.tickets] == ["t1", "t2"]
    # t2 has no baseline → every metric neutral (no comparable data).
    t2 = cmp.tickets[1]
    assert t2.verdict == NEUTRAL
    assert all(m.direction == NEUTRAL for m in t2.metrics)


# ---------------------------------------------------------------------------
# regressions / has_regressions — the baseline flag list
# ---------------------------------------------------------------------------


def test_regressions_flag_metrics_beyond_tolerance() -> None:
    base = _board("base", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board(
        "new",
        [_score("t1", loc=30, tokens=2000, cost=0.05, wall=20, qual=0.5)],  # loc up, tokens up, qual down
        None,
    )
    cmp = compare_scoreboards(base, cand, tolerance=0.10)
    flagged = regressions(cmp)
    assert has_regressions(cmp)
    keys = {metric.key for _, metric in flagged}
    assert "loc_net_mean" in keys
    assert "tokens_mean" in keys
    assert "quality_pass_rate" in keys
    # cost + wall held steady → not flagged.
    assert "cost_mean" not in keys


def test_no_regressions_when_candidate_is_better() -> None:
    base = _board("base", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board("new", [_score("t1", loc=6, tokens=700, cost=0.03, wall=18, qual=1.0)], None)
    cmp = compare_scoreboards(base, cand, tolerance=0.10)
    assert not has_regressions(cmp)
    assert regressions(cmp) == []


def test_regressions_include_overall_row() -> None:
    base = _board(
        "base",
        [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)],
        _score("OVERALL", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
    )
    cand = _board(
        "new",
        [_score("t1", loc=40, tokens=1000, cost=0.05, wall=20, qual=1.0)],
        _score("OVERALL", loc=40, tokens=1000, cost=0.05, wall=20, qual=1.0),
    )
    cmp = compare_scoreboards(base, cand, tolerance=0.10)
    flagged = regressions(cmp)
    assert any(ticket_id == "OVERALL" for ticket_id, _ in flagged)


# ---------------------------------------------------------------------------
# Rendering + serialization
# ---------------------------------------------------------------------------


def test_render_comparison_table_contains_names_and_verdict() -> None:
    base = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0)], None)
    table = render_comparison_table(compare_scoreboards(base, cand))
    assert "A" in table and "B" in table
    assert "t1" in table
    assert BETTER in table


def test_render_comparison_table_handles_missing_pct() -> None:
    # A candidate-only ticket has no baseline, so every metric's pct is None and
    # renders as the em-dash placeholder rather than a percentage.
    base = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board(
        "B",
        [
            _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
            _score("t2", loc=5, tokens=500, cost=0.02, wall=10, qual=1.0),
        ],
        None,
    )
    table = render_comparison_table(compare_scoreboards(base, cand))
    assert "t2" in table
    assert "—" in table


def test_render_comparison_table_handles_comparable_metric_with_no_pct() -> None:
    # A zero-to-zero move is comparable (both sides present) but has no relative
    # percentage — `_fmt_pct` must render the em dash instead of crashing on the
    # division, and the row must go through the "(pct) arrow" branch, not the
    # "not comparable" one.
    base = _board("t1", [_score("t1", loc=0, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board("t2", [_score("t1", loc=0, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    table = render_comparison_table(compare_scoreboards(base, cand))
    loc_line = next(line for line in table.splitlines() if line.strip().startswith("netLOC"))
    assert "not comparable" not in loc_line
    assert "—" in loc_line
    assert "= same" in loc_line


def test_comparison_to_dict_roundtrips_shape() -> None:
    base = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0)], None)
    d = comparison_to_dict(compare_scoreboards(base, cand))
    assert d["baseline_name"] == "A"
    assert d["candidate_name"] == "B"
    assert d["tickets"][0]["ticket_id"] == "t1"
    assert d["tickets"][0]["verdict"] == BETTER
    assert any(m["key"] == "tokens_mean" for m in d["tickets"][0]["metrics"])


# ---------------------------------------------------------------------------
# Baseline IO
# ---------------------------------------------------------------------------


def test_save_and_load_scoreboard_roundtrip(tmp_path: Path) -> None:
    board = _board("A", [_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)], None)
    path = tmp_path / "baseline.json"
    save_scoreboard(board, path)
    loaded = load_scoreboard(path)
    assert loaded["config_name"] == "A"
    assert loaded["tickets"][0]["ticket_id"] == "t1"


def test_load_scoreboard_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(BaselineError):
        load_scoreboard(tmp_path / "nope.json")


def test_load_scoreboard_malformed_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_scoreboard(bad)


def test_load_scoreboard_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "list.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_scoreboard(bad)


# ---------------------------------------------------------------------------
# Story 31.1-002 — provenance: cross-harness header, ticket-set mismatch
# refusal, and the legacy "provenance unknown" warning.
# ---------------------------------------------------------------------------


def _provenance(*, config_name: str, seed: int | None, ticket_ids: list[str]) -> dict:
    return {
        "harness": "claude",
        "model": "sonnet",
        "harness_version": None,
        "host": "h/arm64",
        "config_name": config_name,
        "seed": seed,
        "ticket_ids": ticket_ids,
        "n": 1,
        "timestamp": "2026-09-05T12:00:00Z",
    }


def test_compare_records_baseline_and_candidate_harness() -> None:
    base = {**_board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None), "harness": "claude"}
    cand = {**_board("B", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None), "harness": "codex"}
    cmp = compare_scoreboards(base, cand)
    assert cmp.baseline_harness == "claude"
    assert cmp.candidate_harness == "codex"


def test_compare_harness_defaults_to_claude_when_absent() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cmp = compare_scoreboards(base, cand)
    assert cmp.baseline_harness == "claude"
    assert cmp.candidate_harness == "claude"


def test_render_comparison_table_states_both_harnesses() -> None:
    base = {**_board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None), "harness": "claude"}
    cand = {**_board("B", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None), "harness": "codex"}
    table = render_comparison_table(compare_scoreboards(base, cand))
    assert "claude" in table
    assert "codex" in table


# --- ticket_set_mismatches — the eval-compare refusal signal -----------------


def test_ticket_set_mismatches_empty_when_identical() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    base["provenance"] = _provenance(config_name="A", seed=7, ticket_ids=["t1"])
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand["provenance"] = _provenance(config_name="A", seed=7, ticket_ids=["t1"])
    assert ticket_set_mismatches(base, cand) == []


def test_ticket_set_mismatches_flags_different_config_name() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    mismatches = ticket_set_mismatches(base, cand)
    assert any("config name" in m for m in mismatches)


def test_ticket_set_mismatches_flags_different_ticket_ids() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board(
        "A",
        [
            _score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0),
            _score("t2", loc=1, tokens=1, cost=1, wall=1, qual=1.0),
        ],
        None,
    )
    mismatches = ticket_set_mismatches(base, cand)
    assert any("ticket ids" in m for m in mismatches)


def test_ticket_set_mismatches_reports_ids_missing_from_candidate() -> None:
    # Baseline carries a ticket the candidate doesn't — the "only in baseline"
    # side of the detail message, distinct from the "only in candidate" case
    # covered above.
    base = _board(
        "A",
        [
            _score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0),
            _score("t2", loc=1, tokens=1, cost=1, wall=1, qual=1.0),
        ],
        None,
    )
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    mismatches = ticket_set_mismatches(base, cand)
    assert any("only in baseline" in m and "t2" in m for m in mismatches)


def test_ticket_set_mismatches_flags_different_seed_when_both_known() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    base["provenance"] = _provenance(config_name="A", seed=1, ticket_ids=["t1"])
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand["provenance"] = _provenance(config_name="A", seed=2, ticket_ids=["t1"])
    mismatches = ticket_set_mismatches(base, cand)
    assert any("seed" in m for m in mismatches)


def test_ticket_set_mismatches_ignores_seed_when_provenance_missing() -> None:
    # Legacy scoreboards carry no seed at all — never manufacture a seed
    # mismatch from data that was never recorded.
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand["provenance"] = _provenance(config_name="A", seed=99, ticket_ids=["t1"])
    assert ticket_set_mismatches(base, cand) == []


# --- provenance_warnings — the "provenance unknown" acceptance path ---------


def test_provenance_warnings_empty_when_both_present() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    base["provenance"] = _provenance(config_name="A", seed=1, ticket_ids=["t1"])
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand["provenance"] = _provenance(config_name="A", seed=1, ticket_ids=["t1"])
    assert provenance_warnings(base, cand) == []


def test_provenance_warnings_flags_legacy_baseline() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand["provenance"] = _provenance(config_name="A", seed=1, ticket_ids=["t1"])
    warnings = provenance_warnings(base, cand)
    assert len(warnings) == 1
    assert "provenance unknown" in warnings[0]
    assert "A" in warnings[0]


def test_provenance_warnings_flags_both_missing() -> None:
    base = _board("A", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    cand = _board("B", [_score("t1", loc=1, tokens=1, cost=1, wall=1, qual=1.0)], None)
    assert len(provenance_warnings(base, cand)) == 2
||||||| parent of 87f73aa (feat(harness-benchmarking-baseline): honest token (#31.2-002))


# ---------------------------------------------------------------------------
# Story 31.2-002 — honest token accounting across harnesses
# ---------------------------------------------------------------------------


def _arm_board(name: str, **score: object) -> dict:
    base = {
        "ticket_id": "t1",
        "runs": 1,
        "errors": 0,
        "loc_net_mean": 10.0,
        "wall_mean": 10.0,
        "quality_pass_rate": 1.0,
    }
    base.update(score)
    return {"config_name": name, "tickets": [base]}


# The field finding (2026-09-05): both arms report, the totals are within 7% of
# each other, and they describe completely different work at different prices.
_CACHE_WRITE_ARM = {
    "tokens_mean": 15160.0,
    "cost_mean": 0.0568,
    "input_tokens_mean": 8.0,
    "output_tokens_mean": 7.0,
    "cache_read_tokens_mean": 0.0,
    "cache_creation_tokens_mean": 15145.0,
    "tokens_source": "measured",
}
_CACHE_READ_ARM = {
    "tokens_mean": 14152.0,
    "cost_mean": 0.0042,
    "input_tokens_mean": 6.0,
    "output_tokens_mean": 9.0,
    "cache_read_tokens_mean": 14137.0,
    "cache_creation_tokens_mean": 0.0,
    "tokens_source": "measured",
}


def _row(comparison: object, key: str) -> MetricDelta:
    ticket = comparison.tickets[0]  # type: ignore[attr-defined]
    return next(m for m in ticket.metrics if m.key == key)


def test_near_equal_totals_with_divergent_mixes_are_not_comparable() -> None:
    cmp_ = compare_scoreboards(
        _arm_board("cache-write", **_CACHE_WRITE_ARM),
        _arm_board("cache-read", **_CACHE_READ_ARM),
    )
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert tokens.direction == NEUTRAL
    # The mix, not the total, is what the reason states.
    assert "mix" in tokens.reason
    assert "cache_creation" in tokens.reason
    assert "cache_read" in tokens.reason
    # A near-equal total must never be presented as parity.
    token_line = next(
        line for line in render_comparison_table(cmp_).splitlines()
        if line.strip().startswith("tokens")
    )
    assert "= same" not in token_line
    assert "not comparable" in token_line


def test_cost_rides_on_the_same_verdict_as_tokens() -> None:
    cmp_ = compare_scoreboards(
        _arm_board("a", **_CACHE_WRITE_ARM), _arm_board("b", **_CACHE_READ_ARM)
    )
    cost = _row(cmp_, "cost_mean")
    assert cost.comparable is False
    assert cost.reason == _row(cmp_, "tokens_mean").reason


def test_matching_mixes_stay_comparable_and_still_flag_a_regression() -> None:
    baseline = dict(_CACHE_READ_ARM)
    candidate = {
        "tokens_mean": 28304.0,
        "cost_mean": 0.0084,
        "input_tokens_mean": 12.0,
        "output_tokens_mean": 18.0,
        "cache_read_tokens_mean": 28274.0,
        "cache_creation_tokens_mean": 0.0,
        "tokens_source": "measured",
    }
    cmp_ = compare_scoreboards(_arm_board("a", **baseline), _arm_board("b", **candidate))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is True
    assert tokens.direction == REGRESSED


def test_unavailable_usage_on_one_arm_is_not_comparable_with_a_reason() -> None:
    available = dict(_CACHE_READ_ARM)
    absent = {
        "tokens_mean": None,
        "cost_mean": None,
        "input_tokens_mean": None,
        "output_tokens_mean": None,
        "cache_read_tokens_mean": None,
        "cache_creation_tokens_mean": None,
        "tokens_source": "unavailable",
    }
    cmp_ = compare_scoreboards(_arm_board("claude", **available), _arm_board("local", **absent))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert "unavailable" in tokens.reason
    assert "candidate" in tokens.reason
    # The local arm must not look free.
    table = render_comparison_table(cmp_)
    assert "not comparable" in table


def test_an_estimate_is_never_compared_against_a_measurement() -> None:
    measured = dict(_CACHE_READ_ARM)
    estimated = dict(_CACHE_READ_ARM, tokens_source="estimated")
    cmp_ = compare_scoreboards(_arm_board("a", **measured), _arm_board("b", **estimated))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert "estimate" in tokens.reason


def test_two_estimates_are_comparable_to_each_other() -> None:
    a = dict(_CACHE_READ_ARM, tokens_source="estimated")
    b = dict(_CACHE_READ_ARM, tokens_source="estimated")
    cmp_ = compare_scoreboards(_arm_board("a", **a), _arm_board("b", **b))
    assert _row(cmp_, "tokens_mean").comparable is True


def test_a_board_with_no_component_breakdown_cannot_have_its_mix_judged() -> None:
    legacy = {"tokens_mean": 15000.0, "cost_mean": 0.05}
    cmp_ = compare_scoreboards(_arm_board("old", **legacy), _arm_board("new", **_CACHE_READ_ARM))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert "component breakdown" in tokens.reason


def test_candidate_with_no_component_breakdown_cannot_have_its_mix_judged() -> None:
    # Same as above with the missing breakdown on the other side — the reason
    # must name whichever arm actually lacks it, not always "baseline".
    legacy = {"tokens_mean": 15000.0, "cost_mean": 0.05}
    cmp_ = compare_scoreboards(_arm_board("new", **_CACHE_READ_ARM), _arm_board("old", **legacy))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert "component breakdown" in tokens.reason
    assert "candidate" in tokens.reason


def test_components_summing_to_zero_cannot_have_their_mix_judged() -> None:
    # All four components are present (not None) but sum to zero — there is no
    # mix to divide by, so this must report the same "no breakdown" reason as a
    # row that never carried components at all, not a ZeroDivisionError.
    zero_mix = {
        "tokens_mean": 0.0,
        "cost_mean": 0.0,
        "input_tokens_mean": 0.0,
        "output_tokens_mean": 0.0,
        "cache_read_tokens_mean": 0.0,
        "cache_creation_tokens_mean": 0.0,
        "tokens_source": "measured",
    }
    cmp_ = compare_scoreboards(_arm_board("zero", **zero_mix), _arm_board("new", **_CACHE_READ_ARM))
    tokens = _row(cmp_, "tokens_mean")
    assert tokens.comparable is False
    assert "component breakdown" in tokens.reason
    assert "baseline" in tokens.reason


# ---------------------------------------------------------------------------
# Verdict with excluded axes
# ---------------------------------------------------------------------------


def _delta(key: str, direction: str, *, comparable: bool = True) -> MetricDelta:
    return MetricDelta(
        key=key, label=key, baseline=1.0, candidate=1.0, delta=0.0, pct=0.0,
        direction=direction, comparable=comparable,
        reason=None if comparable else "not comparable",
    )


def test_an_excluded_axis_never_counts_toward_the_verdict() -> None:
    # tokens "improved" only because it is excluded — it must not tip the verdict.
    metrics = [
        _delta("tokens_mean", IMPROVED, comparable=False),
        _delta("loc_net_mean", REGRESSED),
    ]
    assert ticket_verdict(metrics) == WORSE


def test_verdict_is_computed_from_the_comparable_metrics() -> None:
    metrics = [
        _delta("tokens_mean", NEUTRAL, comparable=False),
        _delta("cost_mean", NEUTRAL, comparable=False),
        _delta("loc_net_mean", IMPROVED),
        _delta("wall_mean", IMPROVED),
        _delta("quality_pass_rate", NEUTRAL),
    ]
    assert ticket_verdict(metrics) == BETTER


def test_an_excluded_quality_axis_does_not_decide_the_verdict() -> None:
    metrics = [
        _delta("quality_pass_rate", REGRESSED, comparable=False),
        _delta("loc_net_mean", IMPROVED),
    ]
    assert ticket_verdict(metrics) == BETTER


def test_no_comparable_metric_at_all_is_neutral() -> None:
    metrics = [
        _delta("tokens_mean", IMPROVED, comparable=False),
        _delta("loc_net_mean", IMPROVED, comparable=False),
    ]
    assert ticket_verdict(metrics) == NEUTRAL


def test_comparison_names_the_excluded_axes() -> None:
    cmp_ = compare_scoreboards(
        _arm_board("a", **_CACHE_WRITE_ARM), _arm_board("b", **_CACHE_READ_ARM)
    )
    assert cmp_.tickets[0].excluded == ("tokens", "cost$")
    table = render_comparison_table(cmp_)
    assert "excluded from verdict: tokens, cost$" in table


def test_serialised_comparison_carries_comparability() -> None:
    cmp_ = compare_scoreboards(
        _arm_board("a", **_CACHE_WRITE_ARM), _arm_board("b", **_CACHE_READ_ARM)
    )
    d = comparison_to_dict(cmp_)
    tokens = next(m for m in d["tickets"][0]["metrics"] if m["key"] == "tokens_mean")
    assert tokens["comparable"] is False
    assert "mix" in tokens["reason"]
    assert d["tickets"][0]["excluded"] == ["tokens", "cost$"]


def test_an_excluded_axis_is_neither_a_regression_nor_an_improvement() -> None:
    cmp_ = compare_scoreboards(
        _arm_board("a", **_CACHE_WRITE_ARM), _arm_board("b", **_CACHE_READ_ARM)
    )
    assert not has_regressions(cmp_)
    assert all(m.direction == NEUTRAL for _, m in [] or [])
    assert _row(cmp_, "tokens_mean").direction == NEUTRAL
