# ABOUTME: Parses and validates agent result blocks against JSON-schema contracts.
# ABOUTME: Story 7.2-001 — marker extraction + draft 2020-12 validation with actionable errors.

from __future__ import annotations

import json
import re
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


# A markdown code fence: ```` ``` ```` or ```` ```json ```` opener line, then the
# body up to the closing ````. A long agent run routinely defaults to fenced JSON
# instead of the sentinels, so this is the first fallback when the markers are
# absent. ``[^\n`]*`` matches an optional language tag without crossing the line.
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


def _try_json_object(payload: str) -> dict[str, Any] | None:
    """Parse ``payload`` and return it only if it is a JSON object, else None."""
    payload = payload.strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _balanced_objects(text: str) -> list[str]:
    """Return the top-level balanced ``{ ... }`` substrings of ``text``, in order.

    String/escape aware (braces inside JSON strings do not change depth), so a
    bare result object embedded in prose can be recovered when no fence or
    sentinel is present. Nested objects are part of their enclosing top-level
    object, not separate entries.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc = 0, False, False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[i : j + 1])
                    break
            j += 1
        else:
            # No matching close for the brace at i — nothing more to find.
            break
        i = j + 1
    return out


def _fallback_candidates(response: str) -> list[dict[str, Any]]:
    """Best-effort result objects when the sentinel markers are absent (R10).

    Returns JSON-object candidates best-first: fenced ```` ```json ```` blocks
    last-first (the real result is emitted last), then bare balanced objects
    last-first. De-duplicated. Empty when nothing parses to an object.
    """
    candidates: list[dict[str, Any]] = []

    def _add(obj: dict[str, Any] | None) -> None:
        if obj is not None and obj not in candidates:
            candidates.append(obj)

    for body in reversed(_FENCE_RE.findall(response)):
        _add(_try_json_object(body))
    for blob in reversed(_balanced_objects(response)):
        _add(_try_json_object(blob))
    return candidates


def _parse_sentinel(response: str, start: int) -> dict[str, Any]:
    """Strict parse of the ``<<<RESULT_JSON>>> ... <<<END_RESULT>>>`` block.

    Used when the start marker is present — the agent intended the sentinels, so
    a malformed block yields a precise, actionable error rather than silently
    falling back to some other object in the response.
    """
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


def parse_result_block(response: str) -> dict[str, Any]:
    """Extract and parse the agent's JSON result object.

    Preferred source is the ``<<<RESULT_JSON>>> ... <<<END_RESULT>>>`` block; a
    present-but-malformed block raises a precise :class:`ResultBlockError`. When
    the start marker is absent entirely, falls back to the last fenced
    ```` ```json ```` block, then the last bare balanced JSON object (R10), so
    format drift no longer discards a valid result. Raises
    :class:`ResultBlockError` only when nothing parseable is found.
    """
    start = response.find(RESULT_START_MARKER)
    if start != -1:
        return _parse_sentinel(response, start)

    candidates = _fallback_candidates(response)
    if candidates:
        return candidates[0]
    raise ResultBlockError(
        f"missing {RESULT_START_MARKER} marker: the agent must end its response "
        f"with a {RESULT_START_MARKER} ... {RESULT_END_MARKER} block (a "
        f"```json fenced object or a bare JSON object are accepted as fallbacks)."
    )


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

    Combines :func:`parse_result_block` and :func:`validate_response`. With a
    sentinel block, that single object is validated (strict). Without it, the
    fallback candidates (fenced/bare objects, last-first) are tried in order and
    the **first schema-valid** one wins — so an example/decoy object in the prose
    is skipped in favour of the real result (R10). Raises
    :class:`ResultBlockError` or :class:`SchemaValidationError`.
    """
    start = response.find(RESULT_START_MARKER)
    if start != -1:
        return validate_response(agent_type, _parse_sentinel(response, start))

    candidates = _fallback_candidates(response)
    if not candidates:
        # Reuse parse_result_block's actionable "missing marker" message.
        parse_result_block(response)  # raises ResultBlockError

    last_error: SchemaValidationError | None = None
    for candidate in candidates:
        try:
            return validate_response(agent_type, candidate)
        except SchemaValidationError as exc:
            last_error = exc
    assert last_error is not None  # candidates non-empty ⇒ at least one attempt
    raise last_error
