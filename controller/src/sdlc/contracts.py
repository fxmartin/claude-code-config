# ABOUTME: Parses and validates agent result blocks against JSON-schema contracts.
# ABOUTME: Story 7.2-001 — marker extraction + draft 2020-12 validation with actionable errors.

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

# Markers the agent wraps its final JSON result with. The controller scans the
# agent's free-form response for this block and ignores everything else, so
# agents can keep emitting human-readable prose around the structured result.
RESULT_START_MARKER = "<<<RESULT_JSON>>>"
RESULT_END_MARKER = "<<<END_RESULT>>>"

# The published JSON-schema files ship *inside* the `sdlc` package (under
# `sdlc/schemas/`) so they are bundled into the wheel. Resolving them via
# importlib.resources keeps `sdlc validate` working under `uv tool install`,
# where the source tree is gone and only the installed package remains.
# (A `parents[N] / "schemas"` path is fine in a git checkout but raises
# FileNotFoundError for an installed wheel — that was the Epic-07 E2E defect.)
SCHEMA_DIR: Traversable = resources.files(__package__) / "schemas"

# Agent type -> schema filename. Keys are the names the orchestrator dispatches.
AGENT_SCHEMAS: dict[str, str] = {
    "build": "build-agent-response.schema.json",
    "coverage": "coverage-agent-response.schema.json",
    "review": "review-agent-response.schema.json",
    "merge": "merge-agent-response.schema.json",
    "bugfix": "bugfix-agent-response.schema.json",
}


class ContractError(Exception):
    """Base error for any agent-contract problem (parse or validation)."""


class ResultBlockError(ContractError):
    """The agent response did not contain a well-formed result-marker block."""


class SchemaValidationError(ContractError):
    """The parsed JSON object did not satisfy its agent schema.

    The string representation is an actionable message naming the offending
    field and what was wrong, not a bare "validation failed".
    """


def schema_path(agent_type: str) -> Traversable:
    """Return the schema resource for an agent type, or raise ``KeyError``.

    Returns an ``importlib.abc.Traversable`` (a real ``Path`` for a regular
    wheel install) so callers can ``.read_text()`` it regardless of whether the
    package is unpacked on disk or imported from a zip.
    """
    if agent_type not in AGENT_SCHEMAS:
        valid = ", ".join(sorted(AGENT_SCHEMAS))
        raise KeyError(f"unknown agent type {agent_type!r}; expected one of: {valid}")
    return SCHEMA_DIR / AGENT_SCHEMAS[agent_type]


@lru_cache(maxsize=None)
def load_schema(agent_type: str) -> dict[str, Any]:
    """Load and cache the JSON schema for an agent type."""
    resource = schema_path(agent_type)
    return json.loads(resource.read_text(encoding="utf-8"))


def parse_result_block(response: str) -> dict[str, Any]:
    """Extract and parse the JSON object fenced by the result markers.

    Raises :class:`ResultBlockError` with an actionable message when the
    markers are missing, mis-ordered, or the fenced content is not a JSON
    object.
    """
    start = response.find(RESULT_START_MARKER)
    if start == -1:
        raise ResultBlockError(
            f"missing {RESULT_START_MARKER} marker: the agent must end its "
            f"response with a {RESULT_START_MARKER} ... {RESULT_END_MARKER} block."
        )
    end = response.find(RESULT_END_MARKER, start)
    if end == -1:
        raise ResultBlockError(
            f"found {RESULT_START_MARKER} but no closing {RESULT_END_MARKER} marker."
        )

    payload = response[start + len(RESULT_START_MARKER) : end].strip()
    if not payload:
        raise ResultBlockError(
            f"the {RESULT_START_MARKER} ... {RESULT_END_MARKER} block is empty."
        )

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ResultBlockError(
            f"result block is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(parsed, dict):
        raise ResultBlockError(
            f"result block must be a JSON object, got {type(parsed).__name__}."
        )
    return parsed


def _format_validation_error(agent_type: str, error: Any) -> str:
    """Render a jsonschema ``ValidationError`` into an actionable message."""
    if error.validator == "required":
        # jsonschema reports the missing field in the message; surface the
        # field name explicitly so callers (and tests) can key off it.
        missing = sorted(set(error.validator_value) - set(error.instance or {}))
        field = missing[0] if missing else "?"
        return (
            f"{agent_type}-agent response is missing required field "
            f"{field!r}: {error.message}"
        )

    location = "/".join(str(part) for part in error.absolute_path)
    where = f"field {location!r}" if location else "the response root"
    return f"{agent_type}-agent response failed validation at {where}: {error.message}"


def validate_response(agent_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate an already-parsed object against its agent schema.

    Returns the same object on success. Raises :class:`SchemaValidationError`
    with an actionable message (naming the offending field) on failure.
    Unknown ``agent_type`` raises ``KeyError``.
    """
    schema = load_schema(agent_type)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        primary = best_match(errors) or errors[0]
        raise SchemaValidationError(_format_validation_error(agent_type, primary))
    return data


def parse_and_validate(agent_type: str, response: str) -> dict[str, Any]:
    """Extract the result block from ``response`` and validate it.

    Convenience wrapper combining :func:`parse_result_block` and
    :func:`validate_response`. Raises :class:`ResultBlockError` or
    :class:`SchemaValidationError` (both subclasses of :class:`ContractError`).
    """
    data = parse_result_block(response)
    return validate_response(agent_type, data)
