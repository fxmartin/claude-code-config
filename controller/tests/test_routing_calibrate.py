# ABOUTME: Tests for `sdlc routing calibrate` — escalation bars from the fleet's ledgers (Story 34.5-002).
# ABOUTME: Multi-ledger merge + dedup, origin scoping, p75/rework math, below-floor refusal, --write round-trip.

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.calibrate import (
    calibrate,
    load_sample,
    normalize_origin,
    percentile,
    write_thresholds,
)
from sdlc.cli import app
from sdlc.model_routing import load_routing_config
from sdlc.predictor import MIN_GLOBAL_SAMPLE

runner = CliRunner()
ORIGIN = "http://gitlab.test/root/claude-code-config.git"


def _ledger(
    path: Path,
    stories: list[tuple[str, int, bool]],
    *,
    run_id: str | None = None,
    origin: str | None = None,
) -> str:
    """A ledger holding one run of DONE stories: ``(story_id, tokens, reworked)``."""
    ledger = Ledger(path)
    ledger.init()
    run = ledger.run_create("epic-99", "auto")
    if run_id:  # pin the id so two ledgers can describe the *same* run
        with ledger._connect() as conn:
            conn.execute("UPDATE runs SET id = ? WHERE id = ?", (run_id, run))
        run = run_id
    if origin is not None:
        ledger.event_log(run, "", "info", "config", json.dumps({"origin": origin}))
    for sid, tokens, reworked in stories:
        ledger.story_upsert(
            run, sid, "99", sid, "P1", 2, "py", "", None, "DONE",
            ac_count=5, dep_depth=1, scope_proxy=4,
        )
        ledger.stage_start(run, sid, "build", 1)
        ledger.stage_set_usage(
            run, sid, "build", 1, session_id="s", input_tokens=tokens,
            output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.1,
        )
        ledger.stage_finish(run, sid, "build", 1, "DONE")
        if reworked:
            ledger.stage_start(run, sid, "bugfix", 1)
            ledger.stage_finish(run, sid, "bugfix", 1, "DONE")
    return run


def _stories(prefix: str, tokens: list[int], reworked: int = 0):
    return [(f"{prefix}-{n:03d}", t, n < reworked) for n, t in enumerate(tokens)]


def test_percentile_interpolates_linearly() -> None:
    assert percentile([10, 20, 30, 40], 0.75) == 32.5
    assert percentile([5], 0.75) == 5
    assert percentile([1, 2], 1.0) == 2


def test_percentile_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.75)


def test_normalize_origin_ignores_scheme_credentials_and_dot_git() -> None:
    forms = [
        "http://gitlab.test/root/repo.git",
        "https://tok:en@GitLab.test/root/repo",
        "git@gitlab.test:root/repo.git",
        "ssh://git@gitlab.test/root/repo/",
    ]
    assert len({normalize_origin(f) for f in forms}) == 1
    assert normalize_origin("http://gitlab.test/root/other.git") != normalize_origin(forms[0])


def test_merges_two_ledgers(tmp_path: Path) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _ledger(a, _stories("a", [100] * 6))
    _ledger(b, _stories("b", [200] * 5))
    sample = load_sample([a, b], origin=None)
    assert len(sample.rows) == 11
    assert sample.ledgers == 2


def test_dedupes_the_same_run_story_across_ledger_copies(tmp_path: Path) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    stories = _stories("s", [100, 200, 300])
    _ledger(a, stories, run_id="run-shared")
    _ledger(b, stories + _stories("t", [400]), run_id="run-shared")
    sample = load_sample([a, b], origin=None)
    assert len(sample.rows) == 4
    assert sample.duplicates == 3


def test_same_story_id_in_different_runs_is_not_a_duplicate(tmp_path: Path) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _ledger(a, _stories("s", [100, 200]), run_id="run-a")
    _ledger(b, _stories("s", [100, 200]), run_id="run-b")
    assert len(load_sample([a, b], origin=None).rows) == 4


def test_origin_scoping_drops_other_repos_and_keeps_unrecorded(tmp_path: Path) -> None:
    mine, theirs, legacy = (tmp_path / n for n in ("mine.db", "theirs.db", "legacy.db"))
    _ledger(mine, _stories("m", [100] * 3), origin="https://gitlab.test/root/claude-code-config")
    _ledger(theirs, _stories("t", [999] * 4), origin="http://gitlab.test/root/another.git")
    _ledger(legacy, _stories("l", [100] * 2))  # predates origin stamping
    sample = load_sample([mine, theirs, legacy], origin=ORIGIN)
    assert {r["story_id"][0] for r in sample.rows} == {"m", "l"}
    assert sample.out_of_scope == 4


def test_no_current_origin_applies_no_scoping(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _ledger(db, _stories("t", [1] * 3), origin="http://gitlab.test/root/another.git")
    assert len(load_sample([db], origin=None).rows) == 3


def test_unfinished_and_unmeasured_stories_are_not_reconciled(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    run = _ledger(db, _stories("d", [100] * 2))
    ledger = Ledger(db)
    ledger.story_upsert(run, "wip-001", "99", "w", "P1", 2, "py", "", None, "IN_PROGRESS")
    assert {r["story_id"] for r in load_sample([db], origin=None).rows} == {"d-000", "d-001"}


def test_p75_and_rework_rate_math(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    # 12 stories: tokens 100..1200; the first three reworked.
    _ledger(db, _stories("s", [n * 100 for n in range(1, 13)], reworked=3))
    result = calibrate(load_sample([db], origin=None))
    assert result is not None
    assert result.sample_size == 12
    assert result.p75_tokens == 925  # rank 0.75*11 = 8.25 → 900 + 0.25*(1000-900)
    assert result.rework_rate == pytest.approx(0.25)
    # Twice the base rate: "at least twice as likely to rework as the fleet average".
    assert result.rework_threshold == pytest.approx(0.5)


def test_rework_threshold_is_clamped(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [100] * 12, reworked=0))
    low = calibrate(load_sample([db], origin=None))
    assert low is not None and low.rework_threshold == 0.05
    db2 = tmp_path / "y.db"
    _ledger(db2, _stories("s", [100] * 12, reworked=12))
    high = calibrate(load_sample([db2], origin=None))
    assert high is not None and high.rework_threshold == 0.95


def test_below_the_floor_there_is_no_calibration(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [100] * (MIN_GLOBAL_SAMPLE - 1)))
    assert calibrate(load_sample([db], origin=None)) is None


def test_proposed_yaml_loads_as_a_routing_override(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [n * 100 for n in range(1, 13)], reworked=3))
    result = calibrate(load_sample([db], origin=None))
    assert result is not None
    config = load_routing_config("balanced", override_text=result.proposed_yaml())
    assert config is not None
    assert config.predicted_tokens_threshold == result.p75_tokens
    assert config.rework_threshold == result.rework_threshold


# --- --write ---------------------------------------------------------------


def test_write_round_trips_and_preserves_other_keys(tmp_path: Path) -> None:
    target = tmp_path / ".sdlc-model-routing.yaml"
    target.write_text(
        "# why this file exists\nmodel_routing:\n  predict: true\n"
        "  points_threshold: 5\n  rework_threshold: 0.9\n"
    )
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [n * 100 for n in range(1, 13)], reworked=3))
    result = calibrate(load_sample([db], origin=None))
    assert result is not None
    write_thresholds(target, result, ledgers=[db], today=date(2026, 10, 17))
    text = target.read_text()
    assert text.startswith("# why this file exists")
    assert f"# calibrated 2026-10-17 from 12 reconciled stories in: {db}" in text
    section = yaml.safe_load(text)["model_routing"]
    assert section["predict"] is True and section["points_threshold"] == 5
    assert section["predicted_tokens_threshold"] == result.p75_tokens
    assert section["rework_threshold"] == result.rework_threshold
    # Idempotent: a second write replaces rather than stacking keys or comments.
    write_thresholds(target, result, ledgers=[db], today=date(2026, 10, 18))
    again = target.read_text()
    assert again.count("rework_threshold") == 1
    assert again.count("# calibrated") == 1 and "2026-10-18" in again


def test_write_creates_a_missing_file(tmp_path: Path) -> None:
    target = tmp_path / ".sdlc-model-routing.yaml"
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [100] * 12))
    result = calibrate(load_sample([db], origin=None))
    assert result is not None
    write_thresholds(target, result, ledgers=[db], today=date(2026, 10, 17))
    assert yaml.safe_load(target.read_text())["model_routing"]["predicted_tokens_threshold"] == 100


def test_write_refuses_a_file_without_a_routing_block(tmp_path: Path) -> None:
    target = tmp_path / ".sdlc-model-routing.yaml"
    target.write_text("predict: true\n")
    db = tmp_path / "x.db"
    _ledger(db, _stories("s", [100] * 12))
    result = calibrate(load_sample([db], origin=None))
    assert result is not None
    with pytest.raises(ValueError, match="no block-style"):
        write_thresholds(target, result, ledgers=[db], today=date(2026, 10, 17))
    assert target.read_text() == "predict: true\n"


# --- CLI -------------------------------------------------------------------


def _invoke(*args: str, tmp_path: Path):
    return runner.invoke(
        app, ["routing", "calibrate", *args, "--file", str(tmp_path / "routing.yaml")]
    )


def test_cli_reports_sample_p75_rework_and_yaml(tmp_path: Path) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _ledger(a, _stories("a", [100] * 6, reworked=1))
    _ledger(b, _stories("b", [100] * 6, reworked=1))
    result = _invoke("--ledger", str(a), "--ledger", str(b), tmp_path=tmp_path)
    assert result.exit_code == 0, result.output
    assert "sample: 12 reconciled stories" in result.stdout
    assert "p75 actual story tokens: 100" in result.stdout
    assert "rework rate: 17%" in result.stdout
    assert "predicted_tokens_threshold: 100" in result.stdout
    assert not (tmp_path / "routing.yaml").exists()  # no --write, no file


def test_cli_below_floor_says_no_sample_and_write_touches_nothing(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _ledger(db, _stories("a", [100] * 3))
    target = tmp_path / "routing.yaml"
    target.write_text("model_routing:\n  predict: true\n")
    result = _invoke("--ledger", str(db), "--write", tmp_path=tmp_path)
    assert result.exit_code != 0
    assert "no sample" in result.output
    assert target.read_text() == "model_routing:\n  predict: true\n"


def test_cli_write_updates_the_file(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _ledger(db, _stories("a", [100] * 12))
    result = _invoke("--ledger", str(db), "--write", tmp_path=tmp_path)
    assert result.exit_code == 0, result.output
    section = yaml.safe_load((tmp_path / "routing.yaml").read_text())["model_routing"]
    assert section["predicted_tokens_threshold"] == 100


def test_cli_missing_ledger_is_a_usage_error(tmp_path: Path) -> None:
    result = _invoke("--ledger", str(tmp_path / "nope.db"), tmp_path=tmp_path)
    assert result.exit_code == 2
    assert "nope.db" in result.output


def test_cli_requires_a_ledger(tmp_path: Path) -> None:
    assert _invoke(tmp_path=tmp_path).exit_code == 2
