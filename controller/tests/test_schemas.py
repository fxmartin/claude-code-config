# ABOUTME: Test harness for agent I/O JSON-schema contracts (Story 7.2-001).
# ABOUTME: Covers valid pass, missing-required failure with field name, extra-field allowed.

from __future__ import annotations

import json

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
        "root_cause": "off-by-one in pagination cursor: page 2 reused page 1's offset",
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
    present = {
        p.name for p in SCHEMA_DIR.iterdir() if p.name.endswith(".schema.json")
    }
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


# ---------------------------------------------------------------------------
# Story 26.1-001: root_cause is a required, non-empty field of the bugfix
# contract — a response without one routes as malformed, never propagates.
# ---------------------------------------------------------------------------

def test_bugfix_with_root_cause_is_valid() -> None:
    """A bugfix response carrying a root_cause string validates (26.1-001)."""
    data = dict(VALID_RESPONSES["bugfix"])
    assert data["root_cause"]  # the canonical valid response carries one
    assert validate_response("bugfix", data) == data


def test_bugfix_missing_root_cause_fails_naming_field() -> None:
    """A bugfix response without root_cause fails validation, naming the field."""
    data = dict(VALID_RESPONSES["bugfix"])
    del data["root_cause"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_response("bugfix", data)
    assert "root_cause" in str(exc_info.value)


def test_bugfix_empty_root_cause_rejected() -> None:
    """An empty root_cause is a dodge, not a diagnosis — rejected by schema."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["root_cause"] = ""
    with pytest.raises(SchemaValidationError):
        validate_response("bugfix", data)


# ---------------------------------------------------------------------------
# Story 26.2-001: finding_dispositions is an optional array of per-review-finding
# verdicts — implemented, or disputed-with-reasoning. Absent for non-review
# bugfixes; a disputed finding without reasoning is performative and rejected.
# ---------------------------------------------------------------------------

def test_bugfix_finding_dispositions_omitted_still_valid() -> None:
    """finding_dispositions is optional: a build/test bugfix omits it and validates."""
    data = dict(VALID_RESPONSES["bugfix"])
    assert "finding_dispositions" not in data
    assert validate_response("bugfix", data) == data


def test_bugfix_implemented_finding_disposition_valid() -> None:
    """An implemented finding needs no reasoning and validates (26.2-001)."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [
        {"finding": "missing null guard in parse()", "disposition": "implemented"}
    ]
    assert validate_response("bugfix", data) == data


def test_bugfix_disputed_finding_with_reasoning_valid() -> None:
    """A disputed finding carrying concrete reasoning validates (26.2-001)."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [
        {
            "finding": "null deref at line 42",
            "disposition": "disputed",
            "reasoning": "line 42 is guarded by `if node is not None` on line 40",
        }
    ]
    assert validate_response("bugfix", data) == data


def test_bugfix_disputed_finding_without_reasoning_rejected() -> None:
    """A dispute without reasoning is performative, not a refutation — rejected."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [
        {"finding": "null deref at line 42", "disposition": "disputed"}
    ]
    with pytest.raises(SchemaValidationError):
        validate_response("bugfix", data)


def test_bugfix_disputed_finding_empty_reasoning_rejected() -> None:
    """An empty reasoning string on a dispute is a dodge — rejected by schema."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [
        {"finding": "null deref", "disposition": "disputed", "reasoning": ""}
    ]
    with pytest.raises(SchemaValidationError):
        validate_response("bugfix", data)


def test_bugfix_finding_disposition_bad_enum_rejected() -> None:
    """disposition is a closed enum: implemented | disputed only."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [
        {"finding": "x", "disposition": "ignored"}
    ]
    with pytest.raises(SchemaValidationError):
        validate_response("bugfix", data)


def test_bugfix_finding_disposition_missing_finding_rejected() -> None:
    """Every disposition must name its finding so the verdict is traceable."""
    data = dict(VALID_RESPONSES["bugfix"])
    data["finding_dispositions"] = [{"disposition": "implemented"}]
    with pytest.raises(SchemaValidationError):
        validate_response("bugfix", data)


def test_coverage_optional_dep_scan_status_accepted() -> None:
    """The optional dep_scan_status (Story 9.1-002) validates when present."""
    data = dict(VALID_RESPONSES["coverage"])
    data["dep_scan_status"] = "FAIL"
    assert validate_response("coverage", data) == data


def test_coverage_dep_scan_status_enum_violation_fails() -> None:
    """A dep_scan_status outside the PASS|WARN|FAIL enum is rejected."""
    data = dict(VALID_RESPONSES["coverage"])
    data["dep_scan_status"] = "BLOCK"
    with pytest.raises(SchemaValidationError):
        validate_response("coverage", data)


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


# ---------------------------------------------------------------------------
# Issue #233: tolerant sentinel-block extraction (harmless wrapper deviations)
# ---------------------------------------------------------------------------

_BUILD = VALID_RESPONSES["build"]


def test_sentinel_block_with_json_fence_inside_parses() -> None:
    """The JSON inside the sentinels may be wrapped in a ```json fence."""
    body = json.dumps(_BUILD)
    response = f"prose\n{RESULT_START_MARKER}\n```json\n{body}\n```\n{RESULT_END_MARKER}\n"
    assert parse_result_block(response) == _BUILD
    assert parse_and_validate("build", response) == _BUILD


def test_sentinel_block_with_languageless_fence_inside_parses() -> None:
    """A bare ``` fence (no language tag) inside the sentinels is also stripped."""
    body = json.dumps(_BUILD)
    response = f"{RESULT_START_MARKER}\n```\n{body}\n```\n{RESULT_END_MARKER}"
    assert parse_result_block(response) == _BUILD


def test_whole_envelope_wrapped_in_fence_parses() -> None:
    """The agent may fence the entire envelope, markers and all."""
    body = json.dumps(_BUILD)
    response = f"```json\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n```\n"
    assert parse_and_validate("build", response) == _BUILD


def test_sentinel_block_trailing_prose_ignored() -> None:
    """Free-form prose after the closing marker must not fail extraction."""
    body = json.dumps(_BUILD)
    response = (
        f"{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"
        "And that's a wrap — let me know if you need anything else!"
    )
    assert parse_and_validate("build", response) == _BUILD


def test_sentinel_block_extra_whitespace_parses() -> None:
    """Leading/trailing whitespace around the markers and JSON is tolerated."""
    body = json.dumps(_BUILD)
    response = (
        f"\n\n  {RESULT_START_MARKER}   \n\n   {body}   \n\n  {RESULT_END_MARKER}  \n\n"
    )
    assert parse_and_validate("build", response) == _BUILD


def test_duplicate_sentinel_blocks_last_wins() -> None:
    """When the agent restates the block, the LAST well-formed block wins."""
    first = dict(_BUILD, branch_name="feature/first")
    last = dict(_BUILD, branch_name="feature/last")
    response = (
        f"{RESULT_START_MARKER}\n{json.dumps(first)}\n{RESULT_END_MARKER}\n"
        "on reflection, the final answer:\n"
        f"{RESULT_START_MARKER}\n{json.dumps(last)}\n{RESULT_END_MARKER}\n"
    )
    assert parse_result_block(response)["branch_name"] == "feature/last"
    assert parse_and_validate("build", response)["branch_name"] == "feature/last"


def test_duplicate_sentinel_blocks_skip_malformed_last() -> None:
    """A malformed trailing block is skipped in favour of an earlier well-formed one."""
    response = (
        f"{RESULT_START_MARKER}\n{json.dumps(_BUILD)}\n{RESULT_END_MARKER}\n"
        f"{RESULT_START_MARKER}\n{{not json}}\n{RESULT_END_MARKER}\n"
    )
    assert parse_result_block(response) == _BUILD


@pytest.mark.parametrize(
    "response",
    [
        "no result block here, just prose and no json",  # genuinely absent
        f"{RESULT_START_MARKER}\n{{not json}}\n{RESULT_END_MARKER}",  # malformed JSON
    ],
)
def test_sentinel_extraction_still_rejects_garbage(response: str) -> None:
    """Leniency does not weaken rejection of absent or malformed blocks."""
    with pytest.raises(ContractError):
        parse_and_validate("build", response)


def test_sentinel_schema_invalid_still_raises() -> None:
    """A well-formed but schema-invalid sentinel block still fails validation."""
    bad = {"build_status": "SUCCESS"}  # missing branch_name, commit_sha
    response = f"{RESULT_START_MARKER}\n{json.dumps(bad)}\n{RESULT_END_MARKER}\n"
    with pytest.raises(SchemaValidationError):
        parse_and_validate("build", response)


# ---------------------------------------------------------------------------
# R10: tolerant parsing when the sentinel markers are absent (format drift)
# ---------------------------------------------------------------------------

_BUILD_OK = {
    "branch_name": "feature/34.5-003",
    "build_status": "SUCCESS",
    "commit_sha": "e807982f",
}


def test_fallback_parses_json_fence_when_no_markers() -> None:
    """A ```json fenced object (no sentinels) is recovered (the R10 incident)."""
    body = json.dumps(_BUILD_OK)
    response = f"All done. Here is the result:\n```json\n{body}\n```\n"
    assert parse_result_block(response) == _BUILD_OK
    assert parse_and_validate("build", response) == _BUILD_OK


def test_fallback_parses_language_less_fence() -> None:
    """A bare ``` fence (no language tag) is also recovered."""
    response = f"```\n{json.dumps(_BUILD_OK)}\n```"
    assert parse_result_block(response) == _BUILD_OK


def test_fallback_parses_bare_object_in_prose() -> None:
    """A bare balanced JSON object embedded in prose is recovered."""
    response = f"Result below.\n{json.dumps(_BUILD_OK)}\nThanks!"
    assert parse_result_block(response) == _BUILD_OK


def test_fallback_last_fence_wins() -> None:
    """When several fenced objects appear, the last one is the result."""
    decoy = json.dumps({"build_status": "FAILED"})
    real = json.dumps(_BUILD_OK)
    response = f"```json\n{decoy}\n```\nthen finally\n```json\n{real}\n```"
    assert parse_result_block(response) == _BUILD_OK


def test_fallback_skips_schema_invalid_candidate() -> None:
    """parse_and_validate picks the first schema-valid fallback, skipping a decoy.

    The last block is an example wrapper missing required fields; the real,
    schema-valid result is earlier — it must still be selected.
    """
    real = json.dumps(_BUILD_OK)
    example = json.dumps({"note": "put your result here"})
    response = f"```json\n{real}\n```\nexample format:\n```json\n{example}\n```"
    assert parse_and_validate("build", response) == _BUILD_OK


def test_fallback_brace_scanner_ignores_braces_in_strings() -> None:
    """The bare-object scanner is string-aware (braces inside strings don't count)."""
    payload = dict(_BUILD_OK, commit_sha="ab{c}d")  # braces inside a JSON string value
    response = f"noise {json.dumps(payload)} more noise"
    assert parse_result_block(response) == payload


def test_sentinel_preferred_over_fence() -> None:
    """When both a sentinel block and a fence are present, the sentinel wins."""
    fence_obj = dict(_BUILD_OK, branch_name="feature/from-fence")
    sentinel_obj = dict(_BUILD_OK, branch_name="feature/from-sentinel")
    response = (
        f"```json\n{json.dumps(fence_obj)}\n```\n"
        f"{RESULT_START_MARKER}\n{json.dumps(sentinel_obj)}\n{RESULT_END_MARKER}"
    )
    assert parse_result_block(response)["branch_name"] == "feature/from-sentinel"


def test_no_markers_no_json_still_raises() -> None:
    """Truly marker-less, fence-less, JSON-less output is still a ResultBlockError."""
    with pytest.raises(ResultBlockError) as exc_info:
        parse_result_block("the build failed and I gave up, sorry")
    assert RESULT_START_MARKER in str(exc_info.value)


def test_array_fence_is_not_accepted() -> None:
    """A fenced JSON array (not an object) is not a valid result candidate."""
    response = "```json\n[1, 2, 3]\n```"
    with pytest.raises(ResultBlockError):
        parse_result_block(response)


# ---------------------------------------------------------------------------
# Edge cases required by QA gate (Story 7.2-001 coverage)
# ---------------------------------------------------------------------------

def test_schema_is_valid_draft_2020_12() -> None:
    """Every schema in the schemas/ directory is a structurally valid draft 2020-12 schema."""
    from jsonschema import Draft202012Validator

    schema_files = sorted(
        (p for p in SCHEMA_DIR.iterdir() if p.name.endswith(".schema.json")),
        key=lambda p: p.name,
    )
    for schema_file in schema_files:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        # check_schema raises SchemaError when the meta-schema is violated.
        Draft202012Validator.check_schema(schema)


def test_schema_path_returns_correct_path_for_known_type() -> None:
    """schema_path resolves the expected filename for each known agent type."""
    from sdlc.contracts import schema_path

    for agent_type, filename in AGENT_SCHEMAS.items():
        p = schema_path(agent_type)
        assert p.name == filename
        assert p.is_file(), f"Schema file missing: {p}"


def test_schema_path_raises_keyerror_for_unknown_type() -> None:
    """schema_path raises KeyError with an informative message for unknown types."""
    from sdlc.contracts import schema_path

    with pytest.raises(KeyError) as exc_info:
        schema_path("phantom")
    assert "phantom" in str(exc_info.value)
    # The message must list the valid types so the caller knows what to use.
    for valid in sorted(AGENT_SCHEMAS):
        assert valid in str(exc_info.value)


def test_parse_result_block_uses_last_block_when_multiple_present() -> None:
    """When multiple marker blocks appear, the last well-formed one is extracted.

    Agents sometimes restate the result; the final block is the authoritative
    one (issue #233).
    """
    first = {"branch_name": "first", "build_status": "SUCCESS", "commit_sha": "aaa"}
    second = {"branch_name": "second", "build_status": "FAILED", "commit_sha": "bbb"}
    text = (
        f"{RESULT_START_MARKER}\n{json.dumps(first)}\n{RESULT_END_MARKER}\n"
        f"Some inter-block prose.\n"
        f"{RESULT_START_MARKER}\n{json.dumps(second)}\n{RESULT_END_MARKER}\n"
    )
    result = parse_result_block(text)
    assert result == second


def test_parse_result_block_empty_string_raises_result_block_error() -> None:
    """An empty string (e.g. empty stdin) raises ResultBlockError, not an unhandled exception."""
    with pytest.raises(ResultBlockError) as exc_info:
        parse_result_block("")
    # Must mention the missing start marker, not produce a generic traceback.
    assert RESULT_START_MARKER in str(exc_info.value)


def test_parse_result_block_unicode_payload() -> None:
    """Unicode content in the JSON block is parsed correctly."""
    payload = {
        "branch_name": "feature/unicode-中文-éà",
        "build_status": "SUCCESS",
        "commit_sha": "\U0001f680abc123",
    }
    text = f"{RESULT_START_MARKER}\n{json.dumps(payload, ensure_ascii=False)}\n{RESULT_END_MARKER}"
    result = parse_result_block(text)
    assert result["branch_name"] == payload["branch_name"]
    assert result["commit_sha"] == payload["commit_sha"]


def test_parse_result_block_unicode_surrounds_block() -> None:
    """Unicode prose surrounding the marker block does not confuse the parser."""
    payload = {"branch_name": "ok", "build_status": "SUCCESS", "commit_sha": "c0ff33"}
    text = (
        "中文文本éà\n"
        f"{RESULT_START_MARKER}\n{json.dumps(payload)}\n{RESULT_END_MARKER}\n"
        "\U0001f44d done"
    )
    result = parse_result_block(text)
    assert result == payload


def test_validate_command_empty_stdin_exits_nonzero() -> None:
    """`validate build` with empty stdin exits non-zero with a clear message."""
    from typer.testing import CliRunner
    from sdlc.cli import app

    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["validate", "build"], input="")
    assert result.exit_code == 1
    # Must name the missing marker so the caller knows what to fix.
    assert "RESULT_JSON" in result.output


def test_validate_command_unicode_valid_response() -> None:
    """`validate build` correctly handles a unicode agent response."""
    from typer.testing import CliRunner
    from sdlc.cli import app

    cli_runner = CliRunner()
    payload = {
        "branch_name": "feature/élève",
        "build_status": "SUCCESS",
        "commit_sha": "unicode1",
    }
    response = (
        f"élève analyse\n"
        f"{RESULT_START_MARKER}\n{json.dumps(payload, ensure_ascii=False)}\n{RESULT_END_MARKER}\n"
    )
    result = cli_runner.invoke(app, ["validate", "build"], input=response)
    assert result.exit_code == 0, result.output
    # The CLI uses ensure_ascii=True so non-ASCII chars are \uXXXX-escaped.
    # Parse the output as JSON to verify round-trip fidelity instead of string match.
    parsed_output = json.loads(result.output)
    assert parsed_output["branch_name"] == payload["branch_name"]


def test_validate_command_multiple_blocks_uses_last() -> None:
    """`validate build` with multiple marker blocks validates the last one (issue #233)."""
    from typer.testing import CliRunner
    from sdlc.cli import app

    cli_runner = CliRunner()
    first = {"branch_name": "feature/first", "build_status": "SUCCESS", "commit_sha": "aaa111"}
    second = {"branch_name": "feature/second", "build_status": "FAILED", "commit_sha": "bbb222"}
    response = (
        f"{RESULT_START_MARKER}\n{json.dumps(first)}\n{RESULT_END_MARKER}\n"
        f"Some output between blocks.\n"
        f"{RESULT_START_MARKER}\n{json.dumps(second)}\n{RESULT_END_MARKER}\n"
    )
    result = cli_runner.invoke(app, ["validate", "build"], input=response)
    assert result.exit_code == 0, result.output
    assert "feature/second" in result.output


def test_validate_command_malformed_json_in_block() -> None:
    """`validate build` with malformed JSON in the result block exits 1 with location info."""
    from typer.testing import CliRunner
    from sdlc.cli import app

    cli_runner = CliRunner()
    response = f"{RESULT_START_MARKER}\n{{not_json: true}}\n{RESULT_END_MARKER}\n"
    result = cli_runner.invoke(app, ["validate", "build"], input=response)
    assert result.exit_code == 1
    # The error must include location (line/column) info from parse_result_block.
    assert "line" in result.output.lower()


def test_load_schema_is_cached() -> None:
    """load_schema returns the same object on repeated calls (lru_cache active)."""
    schema_a = load_schema("build")
    schema_b = load_schema("build")
    assert schema_a is schema_b
