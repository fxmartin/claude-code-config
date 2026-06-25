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
    regressions,
    render_comparison_table,
    save_scoreboard,
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
