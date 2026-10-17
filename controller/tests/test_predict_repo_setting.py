# ABOUTME: Tests for the repo-level `predict` switch in .sdlc-model-routing.yaml (Story 34.5-002).
# ABOUTME: Honoured locally and in a queue-launched argv, CLI flag wins either way, banner names the source.

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_build import FakeDispatcher, _sample_queue
from test_scheduler import _store

from sdlc.build import (
    Ledger,
    parse_build_args,
    resolve_predict,
    run_build,
)
from sdlc.model_routing import (
    OVERRIDE_FILENAME,
    load_routing_config,
    repo_predict_setting,
    routing_banner,
    routing_snapshot,
)
from sdlc.scheduler import job_argv

REPO_ROOT = Path(__file__).resolve().parents[2]


def _routing_file(directory: Path, body: str) -> Path:
    path = directory / OVERRIDE_FILENAME
    path.write_text(body)
    return path


# --- reading the key -------------------------------------------------------


def test_repo_predict_setting_reads_true_and_false() -> None:
    assert repo_predict_setting(text="model_routing:\n  predict: true\n") is True
    assert repo_predict_setting(text="model_routing:\n  predict: false\n") is False


def test_repo_predict_setting_is_none_when_unset_or_no_file(tmp_path: Path) -> None:
    assert repo_predict_setting(text="model_routing:\n  profile: balanced\n") is None
    assert repo_predict_setting(tmp_path / OVERRIDE_FILENAME) is None
    assert repo_predict_setting() is None


@pytest.mark.parametrize("bad", ["yes please", "1", "[true]"])
def test_a_non_boolean_predict_is_a_hard_error(bad: str) -> None:
    text = f"model_routing:\n  predict: {bad}\n"
    with pytest.raises(ValueError, match="predict"):
        repo_predict_setting(text=text)
    with pytest.raises(ValueError, match="predict"):
        load_routing_config("balanced", override_text=text)


def test_predict_does_not_disturb_the_routing_config() -> None:
    with_key = load_routing_config("balanced", override_text="model_routing:\n  predict: true\n")
    assert with_key == load_routing_config("balanced")


def test_this_repos_routing_file_turns_prediction_on() -> None:
    assert repo_predict_setting(REPO_ROOT / OVERRIDE_FILENAME) is True
    assert load_routing_config("balanced", override_path=REPO_ROOT / OVERRIDE_FILENAME) is not None


# --- resolution precedence -------------------------------------------------


def test_flags_parse_and_record_their_source() -> None:
    assert parse_build_args(["epic-99"]).predict_source == ""
    on = parse_build_args(["epic-99", "--predict"])
    assert (on.predict, on.predict_source) == (True, "--predict")
    off = parse_build_args(["epic-99", "--no-predict"])
    assert (off.predict, off.predict_source) == (False, "--no-predict")


def test_both_flags_together_are_a_conflict() -> None:
    with pytest.raises(ValueError, match="--predict.*--no-predict"):
        parse_build_args(["epic-99", "--predict", "--no-predict"])


def test_repo_file_turns_prediction_on_without_a_flag(tmp_path: Path) -> None:
    path = _routing_file(tmp_path, "model_routing:\n  predict: true\n")
    opts = parse_build_args(["epic-99"])
    resolve_predict(opts, path)
    assert (opts.predict, opts.predict_source) == (True, OVERRIDE_FILENAME)


def test_no_predict_flag_beats_the_repo_file(tmp_path: Path) -> None:
    path = _routing_file(tmp_path, "model_routing:\n  predict: true\n")
    opts = parse_build_args(["epic-99", "--no-predict"])
    resolve_predict(opts, path)
    assert (opts.predict, opts.predict_source) == (False, "--no-predict")


def test_predict_flag_beats_a_repo_file_that_says_false(tmp_path: Path) -> None:
    path = _routing_file(tmp_path, "model_routing:\n  predict: false\n")
    opts = parse_build_args(["epic-99", "--predict"])
    resolve_predict(opts, path)
    assert (opts.predict, opts.predict_source) == (True, "--predict")


def test_no_routing_file_is_identical_to_today(tmp_path: Path) -> None:
    opts = parse_build_args(["epic-99"])
    resolve_predict(opts, tmp_path / OVERRIDE_FILENAME)
    assert (opts.predict, opts.predict_source) == (False, "default")


def test_a_file_without_the_key_is_identical_to_today(tmp_path: Path) -> None:
    path = _routing_file(tmp_path, "model_routing:\n  points_threshold: 5\n")
    opts = parse_build_args(["epic-99"])
    resolve_predict(opts, path)
    assert (opts.predict, opts.predict_source) == (False, "default")


def test_a_queue_launched_argv_honours_the_repo_file(tmp_path: Path) -> None:
    """A fleet job replays `sdlc build <scope> <flags>` — it never carries --predict."""
    path = _routing_file(tmp_path, "model_routing:\n  predict: true\n")
    store = _store(tmp_path)
    job_id = store.add_job(
        repo=str(tmp_path), kind="build", scope="epic-99", options_json=json.dumps(["--auto"])
    )
    argv = job_argv(store.get_job(job_id), resume=False, prefix=["sdlc"])
    assert "--predict" not in argv
    opts = parse_build_args(argv[2:])
    resolve_predict(opts, path)
    assert opts.predict is True and opts.predict_source == OVERRIDE_FILENAME


# --- the banner and the run ------------------------------------------------


def test_banner_says_where_the_setting_came_from() -> None:
    config = load_routing_config("balanced")
    snap = routing_snapshot(config, predict=True, predict_source=OVERRIDE_FILENAME)
    assert f"model routing: prediction on (from {OVERRIDE_FILENAME})" in routing_banner(snap)
    off = routing_snapshot(config, predict=False, predict_source="default")
    assert "model routing: prediction off (from default)" in routing_banner(off)


def test_banner_is_unchanged_for_a_snapshot_without_a_predict_source() -> None:
    lines = routing_banner(routing_snapshot(load_routing_config("balanced")))
    assert not any("prediction" in line for line in lines)


def _events(db: Path, run_id: str) -> list[str]:
    with Ledger(db)._connect_ro() as conn:
        return [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]


def _build(tmp_path: Path, args: list[str]):
    db = tmp_path / ".sdlc-state.db"
    opts = parse_build_args(["epic-99", "--skip-preflight", "--sequential", "--auto", *args])
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True, root=tmp_path,
    )
    return db, result


def test_run_build_predicts_from_the_repo_file_and_banners_it(tmp_path: Path) -> None:
    _routing_file(tmp_path, "model_routing:\n  predict: true\n")
    db, result = _build(tmp_path, [])
    ledger = Ledger(db)
    assert ledger.run_config(result.run_id)["predict"] is True
    assert ledger.story_prediction_rows()
    assert f"model routing: prediction on (from {OVERRIDE_FILENAME})" in _events(db, result.run_id)


def test_run_build_cli_flag_beats_the_repo_file(tmp_path: Path) -> None:
    _routing_file(tmp_path, "model_routing:\n  predict: true\n")
    db, result = _build(tmp_path, ["--no-predict"])
    assert Ledger(db).run_config(result.run_id)["predict"] is False
    assert Ledger(db).story_prediction_rows() == []
    assert "model routing: prediction off (from --no-predict)" in _events(db, result.run_id)


def test_run_build_without_a_routing_file_is_unchanged(tmp_path: Path) -> None:
    db, result = _build(tmp_path, [])
    assert Ledger(db).run_config(result.run_id)["predict"] is False
    assert Ledger(db).story_prediction_rows() == []


def test_run_build_stamps_the_origin_on_the_config_event(tmp_path: Path) -> None:
    """Calibration scopes ledgers by the origin the run was built against."""
    db, result = _build(tmp_path, [])
    assert "origin" in Ledger(db).run_config(result.run_id)
    assert Ledger(db).run_origins() == {result.run_id: None}  # tmp_path has no remote


def test_run_origins_reads_the_stamped_origin(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "x.db")
    ledger.init()
    run = ledger.run_create("epic-1", "auto")
    ledger.event_log(run, "", "info", "config", json.dumps({"origin": "http://h/o/r.git"}))
    legacy = ledger.run_create("epic-2", "auto")
    ledger.event_log(legacy, "", "info", "config", json.dumps({"auto": True}))
    assert ledger.run_origins() == {run: "http://h/o/r.git", legacy: None}


def test_run_origins_on_a_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "absent.db").run_origins() == {}
