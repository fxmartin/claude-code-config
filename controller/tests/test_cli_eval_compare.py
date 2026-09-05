# ABOUTME: CLI tests for `sdlc eval-compare` / `sdlc eval-baseline` (Story 18.1-002) —
# ABOUTME: A/B verdicts, baseline regression flagging (exit 1), --warn-only, and --update.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


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


def _write_board(path: Path, name: str, score: dict, **extra: object) -> Path:
    board = {**_board("out", [score], None), **extra}
    path.write_text(json.dumps(board), encoding="utf-8")
    return path


def _with_breakdown(score: dict) -> dict:
    """A row carrying the component breakdown a post-31.2-002 scoreboard records.

    Both arms get the same mix, so the token/cost axes stay comparable and the
    gate really does check them.
    """
    tokens = score["tokens_mean"]
    return {
        **score,
        "input_tokens_mean": tokens * 0.05,
        "output_tokens_mean": tokens * 0.05,
        "cache_read_tokens_mean": tokens * 0.90,
        "cache_creation_tokens_mean": 0.0,
        "tokens_source": "measured",
    }


def _write_board(path: Path, name: str, score: dict) -> Path:
    path.write_text(
        json.dumps({"config_name": name, "tickets": [score], "overall": score, **extra}),
        encoding="utf-8",
    )
    return path


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


# ---------------------------------------------------------------------------
# eval-compare
# ---------------------------------------------------------------------------


def test_eval_compare_emits_verdict(tmp_path: Path) -> None:
    # Same ticket set (config name "A", ticket t1) — a real baseline-vs-candidate
    # A/B, e.g. two runs of the same eval config on different code/prompts.
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "A", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b)])
    assert result.exit_code == 0
    assert "BETTER" in result.stdout


def test_eval_compare_json_and_out(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "A", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    out = tmp_path / "cmp.json"
    result = runner.invoke(
        app, ["eval-compare", "--baseline", str(a), "--candidate", str(b), "--json", "--out", str(out)]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tickets"][0]["verdict"] == "better"
    # --out persists the same comparison shown on stdout.
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_eval_compare_bad_file_exits_2(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Story 31.1-002 — provenance: ticket-set refusal, --force, cross-harness
# header, legacy "provenance unknown" acceptance.
# ---------------------------------------------------------------------------


def test_eval_compare_refuses_different_config_names(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "B", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b)])
    assert result.exit_code == 2
    assert "refusing to compare" in result.stderr
    assert "config name" in result.stderr


def test_eval_compare_refuses_different_ticket_ids(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b_path = tmp_path / "b.json"
    b_path.write_text(
        json.dumps(
            {
                "config_name": "A",
                "tickets": [
                    _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
                    _score("t2", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0),
                ],
                "overall": None,
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b_path)])
    assert result.exit_code == 2
    assert "ticket ids" in result.stderr


def test_eval_compare_force_proceeds_with_warning(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "B", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    result = runner.invoke(
        app, ["eval-compare", "--baseline", str(a), "--candidate", str(b), "--force"]
    )
    assert result.exit_code == 0
    assert "config name" in result.stderr  # printed as a warning, not a refusal
    assert "BETTER" in result.stdout


def test_eval_compare_same_ticket_set_cross_harness_states_both_in_header(tmp_path: Path) -> None:
    a = _write_board(
        tmp_path / "a.json",
        "A",
        _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0),
        harness="claude",
    )
    b = _write_board(
        tmp_path / "b.json",
        "A",
        _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0),
        harness="codex",
    )
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b)])
    assert result.exit_code == 0
    assert "claude" in result.stdout
    assert "codex" in result.stdout


def test_eval_compare_legacy_scoreboard_accepted_with_warning(tmp_path: Path) -> None:
    # `a` predates provenance entirely; `b` carries a full block. Same ticket
    # set (config name + ticket ids) so this must NOT be a refusal.
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(
        tmp_path / "b.json",
        "A",
        _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0),
        provenance=_provenance(config_name="A", seed=None, ticket_ids=["t1"]),
    )
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b)])
    assert result.exit_code == 0
    assert "provenance unknown" in result.stderr


def test_eval_compare_json_includes_warnings_key(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "A", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert any("provenance unknown" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# eval-baseline
# ---------------------------------------------------------------------------


def test_eval_baseline_clean_exits_0(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 0
    assert "baseline OK" in result.stdout


# Story 31.2-002: an axis the two scoreboards could not be judged on is excluded
# from the verdict — the *gate* has to name it, or "no regressions" silently reads
# as a pass on a metric nobody checked (the story's own failure mode).


def test_eval_baseline_names_the_axes_it_could_not_check(tmp_path: Path) -> None:
    # Neither row carries a component breakdown (a pre-31.2-002 baseline), so the
    # token mix cannot be judged and both usage axes drop out of the check.
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 0
    assert "not compared" in result.stderr
    assert "tokens" in result.stderr
    assert "cost$" in result.stderr
    assert "component breakdown" in result.stderr
    # ...and the OK line must not claim more than it checked.
    assert "baseline OK" in result.stdout
    assert "on the comparable metrics" in result.stdout


def test_eval_baseline_clean_check_of_every_axis_claims_all_metrics(tmp_path: Path) -> None:
    base = _write_board(
        tmp_path / "base.json", "base",
        _with_breakdown(_score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0)),
    )
    cand = _write_board(
        tmp_path / "cand.json", "new",
        _with_breakdown(_score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0)),
    )
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 0
    assert "not compared" not in result.stderr
    assert "on all metrics" in result.stdout


def test_eval_baseline_regression_still_names_the_excluded_axes(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=40, tokens=1000, cost=0.05, wall=20, qual=0.5))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 1
    assert "regressions vs baseline" in result.stderr
    assert "not compared" in result.stderr


def test_eval_baseline_regression_exits_1(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=40, tokens=1000, cost=0.05, wall=20, qual=0.5))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 1
    assert "regressions vs baseline" in result.stderr


def test_eval_baseline_warn_only_exits_0(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=40, tokens=1000, cost=0.05, wall=20, qual=0.5))
    result = runner.invoke(
        app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand), "--warn-only"]
    )
    assert result.exit_code == 0
    assert "regressions vs baseline" in result.stderr


def test_eval_baseline_update_promotes_candidate(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand), "--update"])
    assert result.exit_code == 0
    assert base.exists()
    assert json.loads(base.read_text(encoding="utf-8"))["config_name"] == "new"


def test_eval_baseline_requires_candidate(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base)])
    assert result.exit_code == 2


def test_eval_baseline_bad_candidate_exits_2(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(tmp_path / "nope.json")])
    assert result.exit_code == 2
    assert "error:" in result.stderr


def test_eval_baseline_bad_baseline_exits_2(tmp_path: Path) -> None:
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(tmp_path / "nope.json"), "--candidate", str(cand)])
    assert result.exit_code == 2
    assert "error:" in result.stderr
