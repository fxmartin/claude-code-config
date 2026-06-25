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


def _write_board(path: Path, name: str, score: dict) -> Path:
    path.write_text(
        json.dumps({"config_name": name, "tickets": [score], "overall": score}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# eval-compare
# ---------------------------------------------------------------------------


def test_eval_compare_emits_verdict(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "B", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(b)])
    assert result.exit_code == 0
    assert "BETTER" in result.stdout


def test_eval_compare_json_and_out(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    b = _write_board(tmp_path / "b.json", "B", _score("t1", loc=4, tokens=600, cost=0.02, wall=15, qual=1.0))
    out = tmp_path / "cmp.json"
    result = runner.invoke(
        app, ["eval-compare", "--baseline", str(a), "--candidate", str(b), "--json", "--out", str(out)]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tickets"][0]["verdict"] == "better"
    # --out persists the same comparison.
    assert json.loads(out.read_text(encoding="utf-8"))["candidate_name"] == "B"


def test_eval_compare_bad_file_exits_2(tmp_path: Path) -> None:
    a = _write_board(tmp_path / "a.json", "A", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    result = runner.invoke(app, ["eval-compare", "--baseline", str(a), "--candidate", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# eval-baseline
# ---------------------------------------------------------------------------


def test_eval_baseline_clean_exits_0(tmp_path: Path) -> None:
    base = _write_board(tmp_path / "base.json", "base", _score("t1", loc=10, tokens=1000, cost=0.05, wall=20, qual=1.0))
    cand = _write_board(tmp_path / "cand.json", "new", _score("t1", loc=8, tokens=900, cost=0.04, wall=19, qual=1.0))
    result = runner.invoke(app, ["eval-baseline", "--baseline", str(base), "--candidate", str(cand)])
    assert result.exit_code == 0
    assert "baseline OK" in result.stdout


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
