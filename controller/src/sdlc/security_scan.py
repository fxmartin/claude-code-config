# ABOUTME: SAST security scan — semgrep command build, report parsing, severity classification.
# ABOUTME: Story 9.1-001 — turns a semgrep --json report into a CLEAN | WARN | BLOCK gate verdict.

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The rulesets every scan runs, regardless of repo. `p/default` is semgrep's
# curated baseline; `p/owasp-top-ten` adds the web-app security canon. Per-repo
# `.sast-config.yaml` may append more, never remove these.
DEFAULT_RULESETS: tuple[str, ...] = ("p/default", "p/owasp-top-ten")

# semgrep severities, mapped to gate verdicts. ERROR blocks the merge; WARNING
# is advisory; INFO is noise we do not gate on. Compared case-insensitively
# because semgrep has shipped both "ERROR" and "error" across versions.
_BLOCK_SEVERITIES = frozenset({"ERROR"})
_WARN_SEVERITIES = frozenset({"WARNING"})


class SastError(Exception):
    """Base error for the SAST security-scan module."""


class SastReportError(SastError):
    """A semgrep report was not well-formed JSON or had an unexpected shape."""


class SastConfigError(SastError):
    """A per-repo `.sast-config.yaml` was malformed or missing a required field."""


# ---------------------------------------------------------------------------
# Per-repo config (.sast-config.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SastConfig:
    """Per-repo SAST overrides.

    ``suppressions`` maps a semgrep rule ID to the mandatory human reason it is
    ignored. ``extra_rulesets`` are appended to :data:`DEFAULT_RULESETS`.
    """

    suppressions: dict[str, str] = field(default_factory=dict)
    extra_rulesets: list[str] = field(default_factory=list)


def load_sast_config(path: str | Path) -> SastConfig:
    """Load a per-repo ``.sast-config.yaml``; an absent file yields empty config.

    Raises :class:`SastConfigError` when the file is not a mapping or when a
    suppression entry omits its mandatory ``reason`` field.
    """
    config_path = Path(path)
    if not config_path.is_file():
        return SastConfig()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SastConfigError(
            f".sast-config.yaml must be a mapping, got {type(raw).__name__}"
        )

    suppressions: dict[str, str] = {}
    for entry in raw.get("suppress") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            raise SastConfigError("each 'suppress' entry must define an 'id'")
        rule_id = str(entry["id"])
        reason = entry.get("reason")
        if not reason or not str(reason).strip():
            raise SastConfigError(
                f"suppression of {rule_id!r} must include a non-empty 'reason'"
            )
        suppressions[rule_id] = str(reason)

    rulesets_raw = raw.get("rulesets") or []
    if not isinstance(rulesets_raw, list):
        raise SastConfigError("'rulesets' must be a list of semgrep config refs")
    extra_rulesets = [str(r) for r in rulesets_raw]

    return SastConfig(suppressions=suppressions, extra_rulesets=extra_rulesets)


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def build_semgrep_command(
    *,
    report_path: str | Path,
    target: str = ".",
    config: SastConfig | None = None,
) -> list[str]:
    """Render the semgrep argv the coverage gate runs.

    Mirrors the AC: ``semgrep --config=p/default --config=p/owasp-top-ten
    --json --output=$REPORT_PATH``. Per-repo extra rulesets are appended; the
    target path is the final argument.
    """
    rulesets = list(DEFAULT_RULESETS)
    if config is not None:
        rulesets.extend(config.extra_rulesets)

    cmd = ["semgrep"]
    cmd.extend(f"--config={ruleset}" for ruleset in rulesets)
    cmd.append("--json")
    cmd.append(f"--output={report_path}")
    cmd.append(str(target))
    return cmd


# ---------------------------------------------------------------------------
# Report parsing + classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SastFinding:
    """One semgrep finding, normalized to the fields the gate cares about."""

    check_id: str
    severity: str
    path: str
    line: int
    message: str

    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class SastResult:
    """The classified outcome of a single SAST scan."""

    status: str  # CLEAN | WARN | BLOCK
    findings: list[SastFinding]
    suppressed: list[SastFinding]
    counts: dict[str, int]


def _parse_finding(raw: dict[str, Any]) -> SastFinding:
    extra = raw.get("extra") or {}
    start = raw.get("start") or {}
    return SastFinding(
        check_id=str(raw.get("check_id", "")),
        severity=str(extra.get("severity", "")).upper(),
        path=str(raw.get("path", "")),
        line=int(start.get("line", 0) or 0),
        message=str(extra.get("message", "")),
    )


def classify_report(report: str, *, config: SastConfig | None = None) -> SastResult:
    """Parse a semgrep ``--json`` report and reduce it to a gate verdict.

    Findings whose ``check_id`` matches a per-repo suppression are removed from
    the gating set (and recorded in ``suppressed``) before severity is judged:

    - ``BLOCK`` when any remaining finding is ERROR-severity.
    - ``WARN`` when any remaining finding is WARNING-severity (and none ERROR).
    - ``CLEAN`` otherwise (no findings, or only INFO/suppressed).

    Raises :class:`SastReportError` on malformed JSON or an unexpected shape.
    """
    try:
        data = json.loads(report)
    except json.JSONDecodeError as exc:
        raise SastReportError(
            f"semgrep report is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise SastReportError(
            f"semgrep report must be a JSON object, got {type(data).__name__}."
        )

    suppressions = config.suppressions if config is not None else {}

    gating: list[SastFinding] = []
    suppressed: list[SastFinding] = []
    for raw in data.get("results") or []:
        finding = _parse_finding(raw)
        if finding.check_id in suppressions:
            suppressed.append(finding)
        else:
            gating.append(finding)

    counts = dict(Counter(f.severity for f in gating))

    if any(f.severity in _BLOCK_SEVERITIES for f in gating):
        status = "BLOCK"
    elif any(f.severity in _WARN_SEVERITIES for f in gating):
        status = "WARN"
    else:
        status = "CLEAN"

    return SastResult(
        status=status,
        findings=gating,
        suppressed=suppressed,
        counts=counts,
    )
