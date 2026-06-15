# ABOUTME: Vendor-agnostic adversarial reviewer slot — interface, config, dispatch, consensus.
# ABOUTME: Story 8.1-001 — any second LLM/SAST tool plugs in via this contract, not orchestrator code.

from __future__ import annotations

import json
import shlex
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

# The output schema ships inside the package (alongside the Epic-07 agent
# schemas) so it resolves under `uv tool install` where the source tree is gone.
_SCHEMA_FILE = "adversarial-reviewer-response.schema.json"

# The three verdicts any reviewer may return, in escalation order.
VERDICTS: tuple[str, ...] = ("approve", "request_changes", "block")

# Consensus rules the controller knows how to apply. The active rule lives in
# the config file, not the code, so changing it does not require a release.
CONSENSUS_RULES: tuple[str, ...] = ("any_block_majority", "unanimous_approve")
DEFAULT_CONSENSUS = "any_block_majority"


class AdversarialError(Exception):
    """Base error for the adversarial reviewer slot."""


class AdversarialContractError(AdversarialError):
    """A reviewer's response was not well-formed or failed schema validation."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    resource = resources.files(__package__) / "schemas" / _SCHEMA_FILE
    return json.loads(resource.read_text(encoding="utf-8"))


# Exposed for tests and callers that want to introspect the published contract.
REVIEWER_SCHEMA: dict[str, Any] = _load_schema()


# ---------------------------------------------------------------------------
# Interface contract — input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """Pipeline signals the reviewer may weigh alongside the diff."""

    tests_pass: bool
    coverage_pct: float
    review_approved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tests_pass": self.tests_pass,
            "coverage_pct": self.coverage_pct,
            "review_approved": self.review_approved,
        }


@dataclass(frozen=True)
class ReviewRequest:
    """The input handed to every adversarial reviewer.

    Mirrors the AC input contract:
    ``{ pr_number, pr_url, story_id, diff, context }``.
    """

    pr_number: int
    pr_url: str
    story_id: str
    diff: str
    context: ReviewContext

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "story_id": self.story_id,
            "diff": self.diff,
            "context": self.context.to_dict(),
        }


# ---------------------------------------------------------------------------
# Config — registered reviewers + consensus rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerConfig:
    """One registered reviewer from ``adversarial-reviewers.yaml``."""

    name: str
    command: str
    timeout_sec: int
    enabled: bool
    allowed_verdicts: list[str] = field(default_factory=lambda: list(VERDICTS))


def load_reviewers_config(path: str | Path) -> tuple[str, list[ReviewerConfig]]:
    """Parse the reviewer registry.

    Returns ``(consensus_rule, reviewers)``. Raises :class:`AdversarialError`
    when the file is malformed or names an unknown consensus rule.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise AdversarialError(f"reviewer config must be a mapping, got {type(raw).__name__}")

    consensus = raw.get("consensus", DEFAULT_CONSENSUS)
    if consensus not in CONSENSUS_RULES:
        valid = ", ".join(CONSENSUS_RULES)
        raise AdversarialError(
            f"unknown consensus rule {consensus!r}; expected one of: {valid}"
        )

    reviewers_raw = raw.get("reviewers") or {}
    if not isinstance(reviewers_raw, dict):
        raise AdversarialError("'reviewers' must be a mapping of name -> settings")

    reviewers: list[ReviewerConfig] = []
    for name, settings in reviewers_raw.items():
        if not isinstance(settings, dict) or "command" not in settings:
            raise AdversarialError(f"reviewer {name!r} must define a 'command'")
        reviewers.append(
            ReviewerConfig(
                name=str(name),
                command=str(settings["command"]),
                timeout_sec=int(settings.get("timeout_sec", 300)),
                enabled=bool(settings.get("enabled", False)),
                allowed_verdicts=list(settings.get("allowed_verdicts", VERDICTS)),
            )
        )
    return consensus, reviewers


def build_command(config: ReviewerConfig, request: ReviewRequest) -> str:
    """Render a reviewer command template against a request's placeholders."""
    return config.command.format(
        pr_number=request.pr_number,
        pr_url=request.pr_url,
        story_id=request.story_id,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerVerdict:
    """A single reviewer's validated verdict."""

    reviewer_name: str
    verdict: str
    summary: str
    findings: list[dict[str, Any]]
    raw: dict[str, Any]


def parse_reviewer_response(output: str) -> dict[str, Any]:
    """Parse and schema-validate a reviewer's JSON output.

    Raises :class:`AdversarialContractError` with an actionable message naming
    the offending field on any parse or validation failure.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AdversarialContractError(
            f"reviewer output is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise AdversarialContractError(
            f"reviewer output must be a JSON object, got {type(data).__name__}."
        )

    validator = Draft202012Validator(REVIEWER_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        primary = best_match(errors) or errors[0]
        raise AdversarialContractError(_format_error(primary))
    return data


def _format_error(error: Any) -> str:
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance or {}))
        field_name = missing[0] if missing else "?"
        return f"reviewer response is missing required field {field_name!r}: {error.message}"
    location = "/".join(str(part) for part in error.absolute_path)
    where = f"field {location!r}" if location else "the response root"
    return f"reviewer response failed validation at {where}: {error.message}"


def _to_verdict(data: dict[str, Any]) -> ReviewerVerdict:
    return ReviewerVerdict(
        reviewer_name=data["reviewer_name"],
        verdict=data["verdict"],
        summary=data["summary"],
        findings=list(data["findings"]),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


def apply_consensus(verdicts: list[str], rule: str = DEFAULT_CONSENSUS) -> str:
    """Reduce a list of per-reviewer verdicts to a single decision.

    ``any_block_majority`` (default): any ``block`` blocks; otherwise the
    majority verdict wins, with ties resolved toward ``request_changes`` (the
    cautious choice). An empty list fails safe and returns ``block``.

    ``unanimous_approve``: every reviewer must ``approve``, else ``block``.
    """
    if rule not in CONSENSUS_RULES:
        valid = ", ".join(CONSENSUS_RULES)
        raise ValueError(f"unknown consensus rule {rule!r}; expected one of: {valid}")

    if not verdicts:
        # No reviewer produced a verdict — fail safe rather than wave it through.
        return "block"

    if rule == "unanimous_approve":
        return "approve" if all(v == "approve" for v in verdicts) else "block"

    # any_block_majority
    if "block" in verdicts:
        return "block"
    counts = Counter(verdicts)
    top = max(counts.values())
    leaders = [v for v, c in counts.items() if c == top]
    if len(leaders) == 1:
        return leaders[0]
    # Tie: prefer the more cautious verdict.
    return "request_changes" if "request_changes" in leaders else leaders[0]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdversarialResult:
    """The aggregate outcome of dispatching all enabled reviewers."""

    consensus: str
    consensus_rule: str
    verdicts: list[ReviewerVerdict]


# The seam tests mock so no real reviewer subprocess is launched: takes the
# rendered command and a timeout, returns the reviewer's raw stdout.
Invoker = Callable[[str, int], str]


def _default_invoke(command: str, timeout: int) -> str:
    """Run a reviewer command as a subprocess and return its stdout."""
    try:
        completed = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdversarialError(f"reviewer timed out after {timeout}s: {command}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise AdversarialError(f"could not launch reviewer: {command} ({exc})") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AdversarialError(f"reviewer exited {completed.returncode}: {detail}")
    return completed.stdout


def dispatch_adversarial_review(
    pr_number: int,
    story_id: str,
    diff: str,
    context: ReviewContext,
    *,
    pr_url: str = "",
    config_path: str | Path,
    invoke: Invoker | None = None,
) -> AdversarialResult:
    """Read the config, run every enabled reviewer in parallel, apply consensus.

    ``invoke`` is the dispatch seam: tests pass a fake that returns canned
    reviewer output, so no real reviewer subprocess runs in CI. In production it
    defaults to :func:`_default_invoke`.
    """
    runner = invoke if invoke is not None else _default_invoke
    consensus_rule, reviewers = load_reviewers_config(config_path)
    enabled = [r for r in reviewers if r.enabled]

    request = ReviewRequest(
        pr_number=pr_number,
        pr_url=pr_url,
        story_id=story_id,
        diff=diff,
        context=context,
    )

    def _run(config: ReviewerConfig) -> ReviewerVerdict:
        command = build_command(config, request)
        output = runner(command, config.timeout_sec)
        return _to_verdict(parse_reviewer_response(output))

    verdicts: list[ReviewerVerdict] = []
    if enabled:
        with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
            verdicts = list(pool.map(_run, enabled))

    consensus = apply_consensus([v.verdict for v in verdicts], consensus_rule)
    return AdversarialResult(
        consensus=consensus,
        consensus_rule=consensus_rule,
        verdicts=verdicts,
    )
