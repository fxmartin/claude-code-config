# ABOUTME: Tests for the mypy ratchet module (Issue #586).
# ABOUTME: Covers mypy output parsing, baseline round-trip, and new-vs-known violation gating.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.type_check import (
    BASELINE_VERSION,
    Baseline,
    MypyDiagnostic,
    TypeCheckBaselineError,
    build_baseline,
    compare_to_baseline,
    load_baseline,
    parse_mypy_output,
    render_baseline,
)

# A pair of real-shaped mypy lines: one pre-existing violation we intend to
# baseline, one an agent might introduce on top of it.
_KNOWN = (
    'src/sdlc/build.py:120: error: Missing type parameters for generic type "dict"'
    "  [type-arg]"
)
_NEW = (
    'src/sdlc/cli.py:498: error: Argument 1 to "run_fix" has incompatible type '
    '"FixOptions | FixBatchOptions"; expected "FixOptions"  [arg-type]'
)


def _report(*lines: str, checked: int = 63) -> str:
    """Wrap diagnostic lines in mypy's usual trailing summary line."""
    body = "\n".join(lines)
    count = len(lines)
    if count == 0:
        return f"Success: no issues found in {checked} source files\n"
    return f"{body}\nFound {count} errors in 1 file (checked {checked} source files)\n"


# ---------------------------------------------------------------------------
# Headline AC: a known violation does not fail the build, a new one does
# ---------------------------------------------------------------------------


def test_known_violation_alone_does_not_block() -> None:
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))

    result = compare_to_baseline(parse_mypy_output(_report(_KNOWN)), baseline)

    assert result.status == "CLEAN"
    assert result.new_violations == []


def test_newly_added_violation_blocks() -> None:
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))

    result = compare_to_baseline(parse_mypy_output(_report(_KNOWN, _NEW)), baseline)

    assert result.status == "BLOCK"
    assert [d.code for d in result.new_violations] == ["arg-type"]
    assert result.new_violations[0].path == "src/sdlc/cli.py"


def test_known_violation_survives_moving_to_a_different_line() -> None:
    # The ratchet must key on (file, code, message), never on line numbers —
    # otherwise every unrelated edit above a baselined error re-fails the gate.
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))
    moved = _KNOWN.replace("build.py:120:", "build.py:947:")

    result = compare_to_baseline(parse_mypy_output(_report(moved)), baseline)

    assert result.status == "CLEAN"


def test_same_violation_twice_when_baseline_holds_one_is_new() -> None:
    # Counts matter: duplicating a baselined error is still a new violation.
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))
    twin = _KNOWN.replace("build.py:120:", "build.py:300:")

    result = compare_to_baseline(parse_mypy_output(_report(_KNOWN, twin)), baseline)

    assert result.status == "BLOCK"
    assert len(result.new_violations) == 1


def test_same_message_in_another_file_is_new() -> None:
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))
    elsewhere = _KNOWN.replace("src/sdlc/build.py", "src/sdlc/status.py")

    result = compare_to_baseline(parse_mypy_output(_report(_KNOWN, elsewhere)), baseline)

    assert result.status == "BLOCK"
    assert result.new_violations[0].path == "src/sdlc/status.py"


# ---------------------------------------------------------------------------
# Draining the backlog
# ---------------------------------------------------------------------------


def test_fixing_a_baselined_violation_warns_but_passes() -> None:
    # Draining the backlog must never fail the build, but the stale baseline is
    # reported so the ratchet gets tightened instead of silently drifting.
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN, _NEW)))

    result = compare_to_baseline(parse_mypy_output(_report(_KNOWN)), baseline)

    assert result.status == "WARN"
    assert result.fixed_total == 1
    assert result.new_violations == []


def test_clean_tree_with_empty_baseline_is_clean() -> None:
    result = compare_to_baseline(parse_mypy_output(_report()), Baseline({}))

    assert result.status == "CLEAN"
    assert result.total == 0


def test_new_violation_wins_over_a_fixed_one() -> None:
    # A PR that fixes one backlog item and adds a different error still blocks.
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN)))

    result = compare_to_baseline(parse_mypy_output(_report(_NEW)), baseline)

    assert result.status == "BLOCK"
    assert result.fixed_total == 1


# ---------------------------------------------------------------------------
# Parsing mypy output
# ---------------------------------------------------------------------------


def test_parses_path_line_severity_message_and_code() -> None:
    (diagnostic,) = parse_mypy_output(_report(_KNOWN))

    assert diagnostic == MypyDiagnostic(
        path="src/sdlc/build.py",
        line=120,
        severity="error",
        message='Missing type parameters for generic type "dict"',
        code="type-arg",
    )


def test_parses_column_and_error_end_forms() -> None:
    with_column = 'src/sdlc/a.py:12:4: error: Bad thing  [misc]'
    with_error_end = 'src/sdlc/b.py:12:4:13:9: error: Bad thing  [misc]'

    diagnostics = parse_mypy_output(_report(with_column, with_error_end))

    assert [d.path for d in diagnostics] == ["src/sdlc/a.py", "src/sdlc/b.py"]
    assert {d.line for d in diagnostics} == {12}
    assert {d.code for d in diagnostics} == {"misc"}


def test_notes_and_summary_lines_are_not_violations() -> None:
    diagnostics = parse_mypy_output(
        "src/sdlc/a.py:1: error: Bad thing  [misc]\n"
        'src/sdlc/a.py:1: note: See https://mypy.rtfd.io/en/stable/_refs.html\n'
        "Found 1 error in 1 file (checked 63 source files)\n"
    )

    assert [d.severity for d in diagnostics] == ["error"]


def test_uncoded_error_lines_are_still_captured() -> None:
    # Not every mypy diagnostic carries a bracketed code (e.g. syntax errors).
    (diagnostic,) = parse_mypy_output("src/sdlc/a.py:1: error: invalid syntax\n")

    assert diagnostic.code == ""
    assert diagnostic.message == "invalid syntax"


def test_blank_and_unrecognized_lines_are_ignored() -> None:
    assert parse_mypy_output("\n  \nSuccess: no issues found\nrandom noise\n") == []


def test_message_line_references_are_normalized_in_the_signature() -> None:
    # "already defined on line 12" would otherwise churn the baseline on every
    # unrelated edit above the definition.
    first = 'src/sdlc/a.py:20: error: Name "x" already defined on line 12  [no-redef]'
    later = 'src/sdlc/a.py:44: error: Name "x" already defined on line 31  [no-redef]'

    (a,) = parse_mypy_output(first)
    (b,) = parse_mypy_output(later)

    assert a.signature == b.signature


def test_location_formats_path_and_line() -> None:
    (diagnostic,) = parse_mypy_output(_KNOWN)

    assert diagnostic.location() == "src/sdlc/build.py:120"


# ---------------------------------------------------------------------------
# Baseline file round-trip
# ---------------------------------------------------------------------------


def test_baseline_round_trips_through_render_and_load(tmp_path: Path) -> None:
    baseline = build_baseline(parse_mypy_output(_report(_KNOWN, _NEW)))
    path = tmp_path / ".mypy-baseline.json"
    path.write_text(render_baseline(baseline), encoding="utf-8")

    assert load_baseline(path) == baseline


def test_rendered_baseline_is_sorted_and_newline_terminated() -> None:
    diagnostics = parse_mypy_output(_report(_NEW, _KNOWN))

    text = render_baseline(build_baseline(diagnostics))
    payload = json.loads(text)

    assert text.endswith("\n")
    assert payload["version"] == BASELINE_VERSION
    assert list(payload["violations"]) == sorted(payload["violations"])


def test_missing_baseline_file_loads_as_empty() -> None:
    assert load_baseline(Path("/nonexistent/.mypy-baseline.json")) == Baseline({})


def test_baseline_records_repeated_signatures_as_counts() -> None:
    twin = _KNOWN.replace("build.py:120:", "build.py:300:")

    baseline = build_baseline(parse_mypy_output(_report(_KNOWN, twin)))

    assert list(baseline.violations.values()) == [2]
    assert baseline.total == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_malformed_baseline_json_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(TypeCheckBaselineError, match="not valid JSON"):
        load_baseline(path)


def test_baseline_that_is_not_an_object_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(TypeCheckBaselineError, match="JSON object"):
        load_baseline(path)


def test_baseline_violations_field_not_a_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text(
        json.dumps({"version": BASELINE_VERSION, "violations": ["a|b|c"]}),
        encoding="utf-8",
    )

    with pytest.raises(TypeCheckBaselineError, match="signature -> count"):
        load_baseline(path)


def test_unknown_baseline_version_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text(
        json.dumps({"version": BASELINE_VERSION + 1, "violations": {}}),
        encoding="utf-8",
    )

    with pytest.raises(TypeCheckBaselineError, match="version"):
        load_baseline(path)


def test_non_integer_baseline_count_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text(
        json.dumps({"version": BASELINE_VERSION, "violations": {"a|b|c": "many"}}),
        encoding="utf-8",
    )

    with pytest.raises(TypeCheckBaselineError, match="positive integer"):
        load_baseline(path)


def test_negative_baseline_count_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mypy-baseline.json"
    path.write_text(
        json.dumps({"version": BASELINE_VERSION, "violations": {"a|b|c": 0}}),
        encoding="utf-8",
    )

    with pytest.raises(TypeCheckBaselineError, match="positive integer"):
        load_baseline(path)
