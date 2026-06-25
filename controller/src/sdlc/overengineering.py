# ABOUTME: Over-engineering review lens — operationalizes CLAUDE.md's complexity-check (Story 18.2-001).
# ABOUTME: Produces a structured delete-list on a story's diff; routes per policy (advisory / route-to-simplify).

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

# The output schema ships inside the package (alongside the Epic-07 agent and
# Epic-08 adversarial schemas) so it resolves under `uv tool install` where the
# source tree is gone.
_SCHEMA_FILE = "overengineering-lens-response.schema.json"

# The kinds of over-engineering the lens flags (mirrors the AC's delete-list
# taxonomy). Kept in lockstep with the schema's `category` enum.
LENS_CATEGORIES: tuple[str, ...] = (
    "speculative_abstraction",
    "unused_code",
    "reinvented_wheel",
    "premature_generality",
    "other",
)

# What the controller does with a non-empty delete-list. The active policy lives
# in the config file, not the code, so changing it does not require a release.
POLICIES: tuple[str, ...] = ("advisory", "route_to_simplify")
DEFAULT_POLICY = "advisory"

# Outcome actions the controller branches on.
ACTION_DISABLED = "disabled"  # lens off — behaviour unchanged from before this story
ACTION_CLEAN = "clean"  # ran, found nothing — already-minimal diff, stay quiet
ACTION_ADVISORY = "advisory"  # record the delete-list on the PR, never block
ACTION_ROUTE_TO_SIMPLIFY = "route_to_simplify"  # hand cuts to the bounded bugfix loop


class OverEngineeringError(Exception):
    """Base error for the over-engineering lens."""


class OverEngineeringContractError(OverEngineeringError):
    """The lens response was not well-formed or failed schema validation."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    resource = resources.files(__package__) / "schemas" / _SCHEMA_FILE
    return json.loads(resource.read_text(encoding="utf-8"))


# Exposed for tests and callers that want to introspect the published contract.
LENS_SCHEMA: dict[str, Any] = _load_schema()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LensConfig:
    """The lens settings from ``overengineering-lens.yaml``."""

    enabled: bool
    policy: str
    command: str = ""
    timeout_sec: int = 300


def load_lens_config(path: str | Path) -> LensConfig:
    """Parse the lens config.

    Raises :class:`OverEngineeringError` when the file is malformed or names an
    unknown policy. Disabled by default so an absent/partial config is inert.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise OverEngineeringError(
            f"lens config must be a mapping, got {type(raw).__name__}"
        )

    policy = raw.get("policy", DEFAULT_POLICY)
    if policy not in POLICIES:
        valid = ", ".join(POLICIES)
        raise OverEngineeringError(
            f"unknown lens policy {policy!r}; expected one of: {valid}"
        )

    return LensConfig(
        enabled=bool(raw.get("enabled", False)),
        policy=str(policy),
        command=str(raw.get("command", "")),
        timeout_sec=int(raw.get("timeout_sec", 300)),
    )


def build_command(config: LensConfig, *, pr_number: int, pr_url: str, story_id: str) -> str:
    """Render the lens command template against a request's placeholders."""
    return config.command.format(
        pr_number=pr_number,
        pr_url=pr_url,
        story_id=story_id,
    )


# ---------------------------------------------------------------------------
# Findings — the delete-list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One over-engineering finding: what to delete, where, and why."""

    category: str
    file: str
    line: int | None
    reason: str

    def format_line(self) -> str:
        """A single human-readable bullet for a PR comment or bugfix directive."""
        loc = f"{self.file}:{self.line}" if self.line is not None else self.file
        return f"- {loc} [{self.category}] {self.reason}"


def parse_lens_response(output: str) -> dict[str, Any]:
    """Parse and schema-validate the lens's JSON output.

    Raises :class:`OverEngineeringContractError` with an actionable message
    naming the offending field on any parse or validation failure.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OverEngineeringContractError(
            f"lens output is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise OverEngineeringContractError(
            f"lens output must be a JSON object, got {type(data).__name__}."
        )

    validator = Draft202012Validator(LENS_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        primary = best_match(errors) or errors[0]
        raise OverEngineeringContractError(_format_error(primary))
    return data


def _format_error(error: Any) -> str:
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance or {}))
        field_name = missing[0] if missing else "?"
        return f"lens response is missing required field {field_name!r}: {error.message}"
    location = "/".join(str(part) for part in error.absolute_path)
    where = f"field {location!r}" if location else "the response root"
    return f"lens response failed validation at {where}: {error.message}"


def extract_findings(data: dict[str, Any]) -> list[Finding]:
    """Lift the validated delete-list into structured :class:`Finding` objects."""
    return [
        Finding(
            category=item["category"],
            file=item["file"],
            line=item["line"],
            reason=item["reason"],
        )
        for item in data["findings"]
    ]


# ---------------------------------------------------------------------------
# Policy routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LensOutcome:
    """The lens decision the controller acts on."""

    action: str
    findings: list[Finding]
    summary: str

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def advisory_comment(self) -> str:
        """The delete-list rendered as an advisory PR comment.

        Empty when there is nothing to flag — the lens stays quiet on
        already-minimal diffs rather than posting noise.
        """
        if not self.findings:
            return ""
        bullets = "\n".join(f.format_line() for f in self.findings)
        return (
            "**Over-engineering lens — delete-list (advisory)**\n\n"
            f"{self.summary}\n\n"
            f"{bullets}\n"
        )

    def simplify_directive(self) -> str:
        """A failure-style string the bounded bugfix loop can act on.

        Reuses the bugfix path: the agent applies these cuts, then the gates
        re-run. Empty when there is nothing to simplify.
        """
        if not self.findings:
            return ""
        bullets = "\n".join(f.format_line() for f in self.findings)
        return (
            "Over-engineering lens flagged over-built code to remove "
            "(smallest reasonable diff). Apply these cuts where they hold the "
            "behaviour and tests:\n"
            f"{bullets}"
        )


def route_findings(
    findings: list[Finding], config: LensConfig, summary: str = ""
) -> LensOutcome:
    """Map findings + config to a single :class:`LensOutcome`.

    - lens disabled        -> ``disabled`` (findings dropped; behaviour unchanged)
    - no findings          -> ``clean`` (already-minimal diff; stay quiet)
    - findings, advisory   -> ``advisory`` (record on PR; never blocks shipping)
    - findings, route_to_simplify -> ``route_to_simplify`` (bounded bugfix loop)
    """
    if not config.enabled:
        return LensOutcome(action=ACTION_DISABLED, findings=[], summary=summary)
    if not findings:
        return LensOutcome(action=ACTION_CLEAN, findings=[], summary=summary)
    action = (
        ACTION_ROUTE_TO_SIMPLIFY
        if config.policy == "route_to_simplify"
        else ACTION_ADVISORY
    )
    return LensOutcome(action=action, findings=list(findings), summary=summary)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def render_lens_prompt(story_id: str, diff: str) -> str:
    """Render the lens agent's instructions for one story's diff.

    Mirrors the built-in ``simplify``/``roast`` skills but runs *in* the
    autonomous pipeline. Asks for a structured delete-list and to stay quiet
    when the diff is already lean (low false-positive rate).
    """
    return (
        f"You are the over-engineering review lens for story {story_id}.\n"
        "Read the diff below and return a structured delete-list of over-built "
        "code: speculative abstractions, unused params/branches, hand-rolled "
        "code a stdlib/existing-dep/one-liner would cover, and premature "
        "generality. For each finding give the file, line, a category, and a "
        "one-line 'why'.\n"
        "Only flag code that is genuinely over-engineered. If the diff is "
        "already minimal and lean, return an empty findings list and say so — "
        "do NOT nitpick code that is already simple.\n\n"
        "DIFF:\n"
        f"{diff}\n\n"
        "End your reply with EXACTLY this wrapper — the literal marker lines, "
        "no markdown code fences:\n"
        "<<<RESULT_JSON>>>\n"
        "{ ...the JSON object per controller/src/sdlc/schemas/"
        "overengineering-lens-response.schema.json ... }\n"
        "<<<END_RESULT>>>"
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


# The seam tests mock so no real lens subprocess is launched: takes the rendered
# command and a timeout, returns the lens's raw stdout.
Invoker = Callable[[str, int], str]


def _default_invoke(command: str, timeout: int) -> str:
    """Run the lens command as a subprocess and return its stdout."""
    try:
        completed = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OverEngineeringError(
            f"lens timed out after {timeout}s: {command}"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise OverEngineeringError(f"could not launch lens: {command} ({exc})") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OverEngineeringError(f"lens exited {completed.returncode}: {detail}")
    return completed.stdout


def dispatch_overengineering_lens(
    pr_number: int,
    story_id: str,
    diff: str,
    *,
    pr_url: str = "",
    config_path: str | Path,
    invoke: Invoker | None = None,
) -> LensOutcome:
    """Read the config, run the lens if enabled, parse, and route per policy.

    Short-circuits to ``disabled`` without invoking anything when the lens is
    off — no quota spend, behaviour unchanged from before this story.

    ``invoke`` is the dispatch seam: tests pass a fake that returns canned lens
    output, so no real subprocess runs in CI. In production it defaults to
    :func:`_default_invoke`.
    """
    config = load_lens_config(config_path)
    if not config.enabled:
        return route_findings([], config)

    runner = invoke if invoke is not None else _default_invoke
    command = build_command(
        config, pr_number=pr_number, pr_url=pr_url, story_id=story_id
    )
    output = runner(command, config.timeout_sec)
    data = parse_lens_response(output)
    findings = extract_findings(data)
    return route_findings(findings, config, summary=data.get("summary", ""))
