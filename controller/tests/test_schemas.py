# ABOUTME: Test harness for agent I/O JSON-schema contracts (Story 7.2-001).
# ABOUTME: Covers valid pass, missing-required failure with field name, extra-field allowed.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.contracts import (
    AGENT_SCHEMAS,
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    SCHEMA_DIR,
    ContractError,
    ResultBlockError,
    SchemaValidationError,
    load_schema,
    parse_and_validate,
    parse_result_block,
    validate_response,
)

# A valid response object for each agent type, matching the published schema.
VALID_RESPONSES: dict[str, dict] = {
    "build": {
        "branch_name": "feature/7.2-001",
        "build_status": "SUCCESS",
        "commit_sha": "abc123def456",
    },
    "coverage": {
        "pr_number": 42,
        "pr_url": "https://github.com/fxmartin/repo/pull/42",
        "coverage_pct": 93.5,
        "tests_added": 7,
        "coverage_status": "PASS",
        "security_status": "PASS",
    },
    "review": {
        "pr_number": 42,
        "approval_status": "APPROVED",
        "change_count": 0,
        "final_status": "APPROVED",
    },
    "merge": {
        "pr_number": 42,
        "merge_status": "MERGED",
        "merge_sha": "deadbeef",
        "merged_at": "2026-06-12T10:30:00Z",
    },
    "bugfix": {
        "failure_category": "TEST_FAILURE",
        "fix_status": "FIXED",
        "tests_passing": True,
        "bugs_fixed": 1,
        "tests_fixed": 3,
    },
}

ALL_AGENT_TYPES = sorted(AGENT_SCHEMAS)


def _wrap(payload: dict) -> str:
    """Wrap a payload in the result-marker block as an agent would."""
    body = json.dumps(payload)
    return f"Some prose.\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\nTrailer."


# ---------------------------------------------------------------------------
# Schema files exist, are draft 2020-12, and are valid JSON
# ---------------------------------------------------------------------------

def test_all_five_schemas_present() -> None:
    """Exactly the five agent schemas named in the AC are published."""
    expected = {
        "build-agent-response.schema.json",
        "coverage-agent-response.schema.json",
        "review-agent-response.schema.json",
        "merge-agent-response.schema.json",
        "bugfix-agent-response.schema.json",
    }
    present = {p.name for p in Path(SCHEMA_DIR).glob("*.schema.json")}
    assert expected.issubset(present)


@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_schema_declares_draft_2020_12(agent_type: str) -> None:
    """Every schema declares the draft 2020-12 dialect."""
    schema = load_schema(agent_type)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_schema_allows_extra_properties(agent_type: str) -> None:
    """Schemas opt into forward-compat by allowing additional properties."""
    schema = load_schema(agent_type)
    assert schema.get("additionalProperties", True) is True


# ---------------------------------------------------------------------------
# AC: valid response passes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_valid_response_passes(agent_type: str) -> None:
    """A well-formed response validates and is returned unchanged."""
    data = VALID_RESPONSES[agent_type]
    assert validate_response(agent_type, data) == data


@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_valid_response_passes_through_marker_block(agent_type: str) -> None:
    """parse_and_validate handles the full marker-wrapped agent response."""
    response = _wrap(VALID_RESPONSES[agent_type])
    assert parse_and_validate(agent_type, response) == VALID_RESPONSES[agent_type]


# ---------------------------------------------------------------------------
# AC: missing required field fails with the field name in the message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_missing_required_field_fails_with_field_name(agent_type: str) -> None:
    """Dropping a required field raises a SchemaValidationError naming it."""
    data = dict(VALID_RESPONSES[agent_type])
    schema = load_schema(agent_type)
    dropped = schema["required"][0]
    del data[dropped]

    with pytest.raises(SchemaValidationError) as exc_info:
        validate_response(agent_type, data)
    message = str(exc_info.value)
    assert dropped in message, f"field name {dropped!r} not in error: {message!r}"
    assert "validation failed" != message.lower()


def test_missing_field_error_is_actionable_not_generic() -> None:
    """The error message is actionable, not a bare 'validation failed'."""
    data = dict(VALID_RESPONSES["build"])
    del data["branch_name"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_response("build", data)
    message = str(exc_info.value)
    assert "branch_name" in message
    assert "build" in message
    assert "missing" in message.lower()


# ---------------------------------------------------------------------------
# AC: extra field is allowed (forward-compat)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_type", ALL_AGENT_TYPES)
def test_extra_field_is_allowed(agent_type: str) -> None:
    """An unknown extra field does not break validation (forward-compat)."""
    data = dict(VALID_RESPONSES[agent_type])
    data["a_future_field_we_dont_know_yet"] = {"nested": [1, 2, 3]}
    assert validate_response(agent_type, data) == data


# ---------------------------------------------------------------------------
# Optional fields and type enforcement
# ---------------------------------------------------------------------------

def test_build_optional_fields_accepted() -> None:
    """Optional build fields (pr_number, error_summary) validate when present."""
    data = dict(VALID_RESPONSES["build"])
    data["build_status"] = "FAILED"
    data["error_summary"] = "tests failed in module x"
    data["pr_number"] = 7
    assert validate_response("build", data) == data


def test_bugfix_optional_issue_number_accepted() -> None:
    """The optional bugfix issue_number validates when present."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["issue_number"] = 314
    assert validate_response("bugfix", data) == data


def test_wrong_type_fails_with_location() -> None:
    """A field with the wrong type fails and the message names the field."""
    data = dict(VALID_RESPONSES["coverage"])
    data["pr_number"] = "not-an-int"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_response("coverage", data)
    assert "pr_number" in str(exc_info.value)


def test_enum_violation_fails() -> None:
    """A value outside an enum is rejected."""
    data = dict(VALID_RESPONSES["build"])
    data["build_status"] = "MAYBE"
    with pytest.raises(SchemaValidationError):
        validate_response("build", data)


def test_unknown_agent_type_raises_keyerror() -> None:
    """Validating against an unknown agent type raises KeyError."""
    with pytest.raises(KeyError):
        validate_response("nonexistent", {"x": 1})


# ---------------------------------------------------------------------------
# Result-marker block parsing
# ---------------------------------------------------------------------------

def test_parse_result_block_extracts_object() -> None:
    """parse_result_block pulls the JSON object out of the marker block."""
    payload = {"branch_name": "feature/x", "build_status": "SUCCESS"}
    parsed = parse_result_block(_wrap(payload))
    assert parsed == payload


def test_parse_result_block_missing_start_marker() -> None:
    """A response with no start marker raises an actionable ResultBlockError."""
    with pytest.raises(ResultBlockError) as exc_info:
        parse_result_block("no markers here at all")
    assert RESULT_START_MARKER in str(exc_info.value)


def test_parse_result_block_missing_end_marker() -> None:
    """A start marker without a matching end marker is reported."""
    text = f"{RESULT_START_MARKER}\n{{}}"
    with pytest.raises(ResultBlockError) as exc_info:
        parse_result_block(text)
    assert RESULT_END_MARKER in str(exc_info.value)


def test_parse_result_block_empty_block() -> None:
    """An empty marker block raises an actionable error."""
    text = f"{RESULT_START_MARKER}\n\n{RESULT_END_MARKER}"
    with pytest.raises(ResultBlockError):
        parse_result_block(text)


def test_parse_result_block_invalid_json_reports_location() -> None:
    """Malformed JSON in the block surfaces line/column, not just 'failed'."""
    text = f"{RESULT_START_MARKER}\n{{not json}}\n{RESULT_END_MARKER}"
    with pytest.raises(ResultBlockError) as exc_info:
        parse_result_block(text)
    message = str(exc_info.value)
    assert "line" in message.lower()


def test_parse_result_block_non_object_rejected() -> None:
    """A JSON array (not an object) in the block is rejected."""
    text = f"{RESULT_START_MARKER}\n[1, 2, 3]\n{RESULT_END_MARKER}"
    with pytest.raises(ResultBlockError):
        parse_result_block(text)


def test_parse_and_validate_propagates_schema_error() -> None:
    """parse_and_validate raises SchemaValidationError on a bad payload."""
    bad = {"build_status": "SUCCESS"}  # missing branch_name, commit_sha
    with pytest.raises(SchemaValidationError):
        parse_and_validate("build", _wrap(bad))


def test_contract_errors_share_base() -> None:
    """ResultBlockError and SchemaValidationError are both ContractError."""
    assert issubclass(ResultBlockError, ContractError)
    assert issubclass(SchemaValidationError, ContractError)
