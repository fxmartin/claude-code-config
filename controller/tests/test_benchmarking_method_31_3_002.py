# ABOUTME: Tests for the benchmarking method / re-run runbook (Story 31.3-002) —
# ABOUTME: proves the method doc exists, covers every required section, and is linked.

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_METHOD_DOC = _REPO_ROOT / "docs" / "harness-benchmarking-method.md"
_EVAL_CONFIG = _REPO_ROOT / "controller" / "eval" / "eval-config.yaml"
_CONTROLLER_ARCH = _REPO_ROOT / "docs" / "controller-architecture.md"
_EVALUATION_DOC = _REPO_ROOT / "docs" / "evaluation.md"


def _doc_text() -> str:
    assert _METHOD_DOC.is_file(), f"missing method doc: {_METHOD_DOC}"
    return _METHOD_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: each metric, its source, its precision, why the two precisions never
# mix in one table.
# ---------------------------------------------------------------------------


def test_doc_states_metric_sources() -> None:
    text = _doc_text().lower()
    for metric in ("quality", "token", "cost", "wall", "loc"):
        assert metric in text, f"missing metric {metric!r}"
    # Sources: ledger (whole-second CURRENT_TIMESTAMP) vs eval time.monotonic().
    assert "current_timestamp" in text or "whole-second" in text
    assert "time.monotonic" in text


def test_doc_explains_why_precisions_are_never_mixed() -> None:
    text = _doc_text().lower()
    assert "never" in text
    # Must name both precision sources in the same explanation.
    assert "ledger" in text and "eval" in text


# ---------------------------------------------------------------------------
# AC2: known non-comparabilities and their causes.
# ---------------------------------------------------------------------------


def test_doc_lists_known_non_comparabilities() -> None:
    text = _doc_text().lower()
    for cause in (
        "telemetry",
        "not-metered",
        "stall",
        "serial",
        "parallel",
    ):
        assert cause in text, f"missing non-comparability cause {cause!r}"


# ---------------------------------------------------------------------------
# AC3: literal re-run command sequence + serving-setup provenance.
# ---------------------------------------------------------------------------


def test_doc_gives_literal_rerun_commands() -> None:
    text = _doc_text()
    assert "sdlc eval" in text
    assert "sdlc eval-compare" in text
    assert re.search(r"```", text), "commands must be in a fenced code block"


def test_doc_lists_serving_setup_provenance_to_capture() -> None:
    text = _doc_text().lower()
    for field in ("quantisation", "context length", "server"):
        assert field in text, f"missing serving-setup provenance field {field!r}"


# ---------------------------------------------------------------------------
# AC4: admission criteria for a future harness joining a comparison.
# ---------------------------------------------------------------------------


def test_doc_states_harness_admission_criteria() -> None:
    text = _doc_text().lower()
    assert "probe" in text
    assert "capabilit" in text  # capability / capabilities
    assert "usage_tracking" in text or "telemetry status" in text


# ---------------------------------------------------------------------------
# DoD: linked from the eval config and the controller docs.
# ---------------------------------------------------------------------------


def test_eval_config_links_the_method_doc() -> None:
    text = _EVAL_CONFIG.read_text(encoding="utf-8")
    assert "harness-benchmarking-method.md" in text


def test_controller_architecture_links_the_method_doc() -> None:
    text = _CONTROLLER_ARCH.read_text(encoding="utf-8")
    assert "harness-benchmarking-method.md" in text


def test_evaluation_doc_links_the_method_doc() -> None:
    text = _EVALUATION_DOC.read_text(encoding="utf-8")
    assert "harness-benchmarking-method.md" in text


def test_doc_is_a_method_note_not_a_results_archive() -> None:
    # Technical note: keep it a method note — 31.3-001's scoreboards are the
    # results and carry their own provenance. A pointer link to them is fine;
    # duplicating their actual figures here is not.
    text = _doc_text()
    assert "199,430" not in text  # 31.3-001's recorded claude token total
    assert "$0.106" not in text  # 31.3-001's recorded claude cost total
