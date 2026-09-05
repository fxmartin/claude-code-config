# ABOUTME: Tests for the recorded claude-vs-local baseline (Story 31.3-001) —
# ABOUTME: proves the committed artifacts are provenance-complete and internally consistent.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.evaluate import load_config
from sdlc.eval_compare import load_scoreboard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVAL_DIR = _REPO_ROOT / "controller" / "eval"
_RESULTS_DIR = _EVAL_DIR / "results"
_FULL_CONFIG = _EVAL_DIR / "eval-config.yaml"

_CLAUDE_SCOREBOARD = _RESULTS_DIR / "claude-scoreboard-31.3-001.json"
_QWEN_FAILURE = _RESULTS_DIR / "qwen-preflight-failure.json"
_COMPARE_OUTPUT = _RESULTS_DIR / "eval-compare-31.3-001.txt"
_GO_NO_GO = _EVAL_DIR / "GO-NO-GO-31.3-001.md"


def test_claude_scoreboard_exists_and_is_provenance_complete() -> None:
    assert _CLAUDE_SCOREBOARD.is_file(), f"missing recorded claude scoreboard: {_CLAUDE_SCOREBOARD}"
    board = load_scoreboard(_CLAUDE_SCOREBOARD)
    assert board["harness"] == "claude"
    provenance = board.get("provenance")
    assert provenance is not None, "claude scoreboard must carry Story 31.1-002 provenance"
    for key in (
        "harness", "model", "host", "config_name", "seed", "ticket_ids", "n", "timestamp",
    ):
        assert key in provenance, f"provenance missing {key!r}"
    assert provenance["harness"] == "claude"


def test_claude_scoreboard_covers_the_identical_ticket_set_at_config_n() -> None:
    config = load_config(_FULL_CONFIG)
    board = load_scoreboard(_CLAUDE_SCOREBOARD)
    provenance = board["provenance"]
    assert provenance["ticket_ids"] == [t.id for t in config.tickets]
    assert provenance["n"] == config.n
    assert config.n >= 3, "AC1 requires n >= 3"
    assert provenance["seed"] == config.seed
    assert provenance["config_name"] == config.name


def test_claude_scoreboard_has_a_row_per_ticket_plus_overall() -> None:
    config = load_config(_FULL_CONFIG)
    board = load_scoreboard(_CLAUDE_SCOREBOARD)
    ticket_ids = {t["ticket_id"] for t in board["tickets"]}
    assert ticket_ids == {t.id for t in config.tickets}
    assert board["overall"] is not None


def test_qwen_failure_record_names_the_identical_ticket_set() -> None:
    config = load_config(_FULL_CONFIG)
    assert _QWEN_FAILURE.is_file(), f"missing recorded qwen failure: {_QWEN_FAILURE}"
    record = json.loads(_QWEN_FAILURE.read_text(encoding="utf-8"))
    assert record["harness"] == "qwen"
    assert record["ticket_ids"] == [t.id for t in config.tickets]
    assert record["n"] == config.n
    assert record["seed"] == config.seed


def test_qwen_failure_record_reports_by_category_not_dropped() -> None:
    # AC4: a local arm that fails outright must be reported with its category,
    # never silently absent from the recorded baseline.
    record = json.loads(_QWEN_FAILURE.read_text(encoding="utf-8"))
    assert record["runs_completed"] == 0
    assert record["failure_category"]
    assert record["stderr_verbatim"], "the real error text must be recorded verbatim"


def test_eval_compare_output_is_recorded() -> None:
    assert _COMPARE_OUTPUT.is_file(), f"missing recorded eval-compare output: {_COMPARE_OUTPUT}"
    text = _COMPARE_OUTPUT.read_text(encoding="utf-8")
    assert "compare:" in text
    assert "tolerance" in text


def test_go_no_go_is_recorded_and_states_serial_discipline() -> None:
    assert _GO_NO_GO.is_file(), f"missing go/no-go record: {_GO_NO_GO}"
    text = _GO_NO_GO.read_text(encoding="utf-8")
    # AC2: a parallel-capable arm must never be compared on wall-clock against a
    # serial local run — the report has to say every arm ran serially.
    assert "serial" in text.lower()
    assert "qwen" in text.lower()
    assert "go/no-go" in text.lower() or "go / no-go" in text.lower()
