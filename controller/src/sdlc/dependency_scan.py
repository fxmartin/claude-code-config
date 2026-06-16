# ABOUTME: Dependency security scan — osv-scanner command build, report parsing, severity classification.
# ABOUTME: Story 9.1-002 — turns an osv-scanner --format=json report into a CLEAN | WARN | BLOCK gate verdict.

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# OSV severity labels (from each vulnerability's `database_specific.severity`)
# mapped to gate verdicts. HIGH/CRITICAL block the merge; LOW/MODERATE are
# advisory. Compared case-insensitively because ecosystems differ on casing
# and OSV mirrors several of them ("MODERATE" from GHSA, "MEDIUM" elsewhere).
_BLOCK_SEVERITIES = frozenset({"HIGH", "CRITICAL"})
_WARN_SEVERITIES = frozenset({"LOW", "MODERATE", "MEDIUM"})

# When a vulnerability carries no severity label, fall back to its CVSS base
# score. A score of 7.0 or above is "high" per the CVSS qualitative bands, so
# it blocks; anything lower (including an unscored finding) is advisory.
_CVSS_BLOCK_THRESHOLD = 7.0


class DepScanError(Exception):
    """Base error for the dependency security-scan module."""


class DepScanReportError(DepScanError):
    """An osv-scanner report was not well-formed JSON or had an unexpected shape."""


class DepScanConfigError(DepScanError):
    """A `.dep-scan-suppressions.yaml` was malformed, missing a required field,
    or contains a suppression that is past its expiry date."""


# ---------------------------------------------------------------------------
# Per-repo suppressions (.dep-scan-suppressions.yaml)
# ---------------------------------------------------------------------------


def load_dep_scan_suppressions(
    path: str | Path, *, today: date | None = None
) -> dict[str, str]:
    """Load per-repo OSV-ID suppressions; an absent file yields ``{}``.

    Returns a mapping of OSV vulnerability ID to its mandatory human reason.
    Every suppression entry MUST carry a non-empty ``reason`` and a valid
    ``expires`` date (ISO ``YYYY-MM-DD``). A suppression whose ``expires`` is in
    the past raises :class:`DepScanConfigError`, so CI fails on a stale
    suppression rather than silently accepting a known-vulnerable dependency.

    ``today`` is injectable for deterministic tests; it defaults to the system
    date.
    """
    config_path = Path(path)
    if not config_path.is_file():
        return {}

    now = today or date.today()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DepScanConfigError(
            f".dep-scan-suppressions.yaml must be a mapping, got {type(raw).__name__}"
        )

    suppressions: dict[str, str] = {}
    for entry in raw.get("suppress") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            raise DepScanConfigError("each 'suppress' entry must define an 'id'")
        osv_id = str(entry["id"])

        reason = entry.get("reason")
        if not reason or not str(reason).strip():
            raise DepScanConfigError(
                f"suppression of {osv_id!r} must include a non-empty 'reason'"
            )

        expires_raw = entry.get("expires")
        if not expires_raw:
            raise DepScanConfigError(
                f"suppression of {osv_id!r} must include an 'expires' date (YYYY-MM-DD)"
            )
        try:
            expires = date.fromisoformat(str(expires_raw))
        except ValueError as exc:
            raise DepScanConfigError(
                f"suppression of {osv_id!r} has a malformed 'expires' date "
                f"{expires_raw!r}; use ISO YYYY-MM-DD."
            ) from exc

        if expires < now:
            raise DepScanConfigError(
                f"suppression of {osv_id!r} expired on {expires.isoformat()}; "
                "renew the review or remove it and bump the dependency."
            )

        suppressions[osv_id] = str(reason)

    return suppressions


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def build_osv_command(
    *,
    report_path: str | Path,
    target: str = ".",
) -> list[str]:
    """Render the osv-scanner argv the coverage gate runs.

    Mirrors the AC: ``osv-scanner --lockfile=auto --format=json
    --output=$REPORT_PATH .``. ``--lockfile=auto`` auto-detects
    ``package-lock.json``, ``uv.lock``, ``poetry.lock``, ``go.sum``,
    ``Cargo.lock`` and the rest of OSV's supported lockfiles. The target path is
    the final argument.
    """
    return [
        "osv-scanner",
        "--lockfile=auto",
        "--format=json",
        f"--output={report_path}",
        str(target),
    ]


# ---------------------------------------------------------------------------
# Report parsing + classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepScanFinding:
    """One osv-scanner vulnerability, normalized to the fields the gate cares about."""

    osv_id: str
    severity: str  # normalized verdict band: BLOCK | WARN
    package: str
    version: str
    ecosystem: str
    summary: str

    def coordinate(self) -> str:
        return f"{self.package}@{self.version}"


@dataclass(frozen=True)
class DepScanResult:
    """The classified outcome of a single dependency scan."""

    status: str  # CLEAN | WARN | BLOCK
    findings: list[DepScanFinding]
    suppressed: list[DepScanFinding]
    counts: dict[str, int]


def _cvss_score(vuln: dict[str, Any]) -> float:
    """Extract the highest CVSS base score from a vulnerability's ``severity[]``.

    OSV CVSS vectors look like ``CVSS:3.1/AV:N/.../C:H/I:H/A:H``; the numeric
    base score is not always present, so we derive a conservative score from the
    impact metrics when only a vector string is given. Returns 0.0 when no usable
    score is found.
    """
    best = 0.0
    for sev in vuln.get("severity") or []:
        if not isinstance(sev, dict):
            continue
        score = sev.get("score")
        if score is None:
            continue
        text = str(score)
        # A bare numeric score (some ecosystems publish "7.5").
        try:
            best = max(best, float(text))
            continue
        except ValueError:
            pass
        # A CVSS vector string: treat a high-impact vector (any of C/I/A == H)
        # as blocking. This is intentionally conservative — a vector we cannot
        # score numerically but that lists a High impact gates the build.
        if "CVSS" in text and any(token in text for token in ("C:H", "I:H", "A:H")):
            best = max(best, _CVSS_BLOCK_THRESHOLD)
    return best


def _classify_vuln_band(vuln: dict[str, Any]) -> str:
    """Reduce one vulnerability to a verdict band: ``BLOCK`` or ``WARN``.

    Uses the ``database_specific.severity`` label first (HIGH/CRITICAL block),
    then falls back to the CVSS score. An unknown or absent label with no usable
    CVSS score is advisory (``WARN``) — a known finding never silently passes.
    """
    label = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
    if label in _BLOCK_SEVERITIES:
        return "BLOCK"
    if label in _WARN_SEVERITIES:
        return "WARN"
    # No usable label: fall back to CVSS.
    if _cvss_score(vuln) >= _CVSS_BLOCK_THRESHOLD:
        return "BLOCK"
    return "WARN"


def classify_osv_report(
    report: str, *, suppressions: dict[str, str] | None = None
) -> DepScanResult:
    """Parse an osv-scanner ``--format=json`` report and reduce it to a verdict.

    Findings whose OSV ID matches a per-repo suppression are removed from the
    gating set (and recorded in ``suppressed``) before severity is judged:

    - ``BLOCK`` when any remaining finding is HIGH/CRITICAL severity.
    - ``WARN`` when any remaining finding is LOW/MODERATE (and none HIGH).
    - ``CLEAN`` otherwise (no findings, or all suppressed).

    Raises :class:`DepScanReportError` on malformed JSON or an unexpected shape.
    """
    try:
        data = json.loads(report)
    except json.JSONDecodeError as exc:
        raise DepScanReportError(
            f"osv-scanner report is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise DepScanReportError(
            f"osv-scanner report must be a JSON object, got {type(data).__name__}."
        )

    suppress = suppressions or {}

    gating: list[DepScanFinding] = []
    suppressed: list[DepScanFinding] = []

    for result in data.get("results") or []:
        for pkg in result.get("packages") or []:
            meta = pkg.get("package") or {}
            name = str(meta.get("name", ""))
            version = str(meta.get("version", ""))
            ecosystem = str(meta.get("ecosystem", ""))
            for vuln in pkg.get("vulnerabilities") or []:
                osv_id = str(vuln.get("id", ""))
                finding = DepScanFinding(
                    osv_id=osv_id,
                    severity=_classify_vuln_band(vuln),
                    package=name,
                    version=version,
                    ecosystem=ecosystem,
                    summary=str(vuln.get("summary", "")),
                )
                if osv_id in suppress:
                    suppressed.append(finding)
                else:
                    gating.append(finding)

    counts = dict(Counter(f.severity for f in gating))

    if any(f.severity == "BLOCK" for f in gating):
        status = "BLOCK"
    elif gating:
        status = "WARN"
    else:
        status = "CLEAN"

    return DepScanResult(
        status=status,
        findings=gating,
        suppressed=suppressed,
        counts=counts,
    )
