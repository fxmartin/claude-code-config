# ABOUTME: Supply-chain pattern scanner — flags dangerous tokens in hooks/skills/MCP/settings.
# ABOUTME: Story 13.2-001 — treats installed config artifacts as supply-chain and gates CLEAN|WARN|BLOCK.

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml


def content_digest(line: str) -> str:
    """Stable content digest of a matched line, binding an allowlist entry to it.

    An allowlist entry stores this digest so suppression is bound to the *exact*
    text the reviewer approved: if the content at that path/line/pattern is later
    swapped (e.g. a benign documented command replaced with a malicious one), the
    digest no longer matches and the gate re-surfaces the finding. The line is
    stripped first so trivial indentation changes do not churn the digest.

    The full 256-bit SHA-256 hex is returned — not a truncation. The attacker
    controls the line content, so a truncated digest would be a second-preimage
    target: with, say, 48 bits a crafted malicious line colliding onto an
    allowlisted digest is only ~2**48 work. The full digest makes that
    infeasible, at the cost of a longer (but copy-pasted) allowlist field.
    """
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------------------
#
# Each pattern is classified into a verdict band:
#
#   BLOCK — high-signal poisoning/exfil markers with essentially no legitimate
#           use in a hook/skill/MCP/settings artifact. A match fails CI.
#   WARN  — suspicious-but-commonly-legitimate markers (plain network tools).
#           Advisory only; the operator reviews and, if intended, allowlists
#           the specific (path, pattern) pair.
#
# The split keeps the existing (clean) repo green — a legitimate `curl` in a
# notification hook is a WARN, not a build break — while the markers that
# really indicate a poisoned config (API redirection, MCP auto-trust, encoded
# payloads, hidden Unicode, pipe-to-shell) hard-fail.

# Zero-width / bidi-control code points used to smuggle hidden instructions:
# ZWSP, ZWNJ, ZWJ, WORD JOINER, BOM/ZWNBSP, and the bidi embedding/override set.
_ZERO_WIDTH = "​-‍⁠﻿‪-‮"


@dataclass(frozen=True)
class Pattern:
    """One dangerous-token rule the scanner matches against each source line."""

    id: str
    regex: re.Pattern[str]
    band: str  # WARN | BLOCK
    description: str


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        id="pipe-to-shell",
        # curl/wget … | sh|bash — the internet piped straight into a shell.
        regex=re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
        band="BLOCK",
        description="downloads and executes a remote script (curl|wget piped to a shell)",
    ),
    Pattern(
        id="mcp-trust-all",
        regex=re.compile(r"enableAllProjectMcpServers"),
        band="BLOCK",
        description="auto-trusts every project MCP server without review",
    ),
    Pattern(
        id="anthropic-base-url",
        regex=re.compile(r"ANTHROPIC_BASE_URL"),
        band="BLOCK",
        description="overrides the Anthropic API endpoint (credential/redirection risk)",
    ),
    Pattern(
        id="data-uri-html",
        regex=re.compile(r"data:text/html"),
        band="BLOCK",
        description="embeds an executable HTML/script payload via a data URI",
    ),
    Pattern(
        id="base64-payload",
        regex=re.compile(r"base64,"),
        band="BLOCK",
        description="carries a base64-encoded payload (obfuscated content)",
    ),
    Pattern(
        id="zero-width-unicode",
        regex=re.compile(f"[{_ZERO_WIDTH}]"),
        band="BLOCK",
        description="contains zero-width or bidi-control Unicode (hidden instructions)",
    ),
    Pattern(
        id="network-egress",
        regex=re.compile(r"\b(?:curl|wget|nc|scp|ssh)\b"),
        band="WARN",
        description="invokes a network/egress tool (curl/wget/nc/scp/ssh)",
    ),
)

# The set of legal pattern ids, used to reject typo'd allowlist entries.
_PATTERN_IDS = frozenset(p.id for p in PATTERNS)

# The fixed scan surface, relative to a repo root. Directories are walked
# recursively; the two named files are scanned when present.
_SCAN_DIRS = ("hooks", "skills")
_SCAN_GLOBS = ("plugins/*/skills",)
_SCAN_FILES = ("mcp/config.template.json", "settings.json")

# Directories never worth scanning even when nested under a target dir.
_SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".venv"})


class SupplyChainScanError(Exception):
    """Base error for the supply-chain scan module."""


class SupplyChainConfigError(SupplyChainScanError):
    """A `.supply-chain-allowlist.yaml` was malformed or missing a required field."""


# ---------------------------------------------------------------------------
# Findings + result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanFinding:
    """One dangerous-pattern match, normalized to the fields the gate reports."""

    pattern_id: str
    band: str  # WARN | BLOCK
    path: str
    line: int
    snippet: str
    description: str
    digest: str  # content_digest() of the matched line — binds allowlist entries

    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class ScanResult:
    """The classified outcome of a single supply-chain scan."""

    status: str  # CLEAN | WARN | BLOCK
    findings: list[ScanFinding]
    suppressed: list[ScanFinding]
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# Per-repo allowlist (.supply-chain-allowlist.yaml)
# ---------------------------------------------------------------------------


def load_allowlist(path: str | Path) -> dict[tuple[str, int, str, str], str]:
    """Load the per-finding allowlist; an absent file yields ``{}``.

    The allowlist is intentionally per-finding, not a blanket disable: each
    entry names a specific ``path`` + ``line`` + ``pattern`` + content
    ``sha256`` and a mandatory human ``reason``. Returns a mapping of ``(path,
    line, pattern_id, sha256)`` to its reason.

    Two properties are deliberate. Keying on the *line* (not just the file)
    means an entry suppresses the one finding the reviewer approved, not every
    same-pattern occurrence in the file. Keying on the content *sha256* means
    the entry is bound to the exact reviewed text: if the line's content is
    later swapped — same path/line/pattern, different payload — the digest no
    longer matches and the gate re-surfaces the finding.

    Raises :class:`SupplyChainConfigError` when the file is not a mapping, an
    entry omits ``path``/``line``/``pattern``/``sha256``/``reason``, ``line`` is
    not a positive integer, or ``pattern`` is not a known pattern id (a typo'd
    id would silently suppress nothing).
    """
    config_path = Path(path)
    if not config_path.is_file():
        return {}

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SupplyChainConfigError(
            f".supply-chain-allowlist.yaml must be a mapping, got {type(raw).__name__}"
        )

    allowlist: dict[tuple[str, int, str, str], str] = {}
    for entry in raw.get("allow") or []:
        if not isinstance(entry, dict):
            raise SupplyChainConfigError(
                "each 'allow' entry must be a mapping with 'path', 'line', "
                "'pattern', 'sha256', 'reason'"
            )

        path_value = entry.get("path")
        if not path_value or not str(path_value).strip():
            raise SupplyChainConfigError(
                "each 'allow' entry must define a non-empty 'path'"
            )

        pattern_value = entry.get("pattern")
        if not pattern_value or not str(pattern_value).strip():
            raise SupplyChainConfigError(
                "each 'allow' entry must define a non-empty 'pattern'"
            )
        pattern_id = str(pattern_value)
        if pattern_id not in _PATTERN_IDS:
            raise SupplyChainConfigError(
                f"allow entry references unknown pattern {pattern_id!r}; "
                f"valid ids: {', '.join(sorted(_PATTERN_IDS))}"
            )

        # ``line`` scopes the entry to a single finding (the one the reviewer
        # signed off on). ``bool`` is rejected explicitly because it is an int
        # subclass — ``line: true`` would otherwise read as line 1.
        line_value = entry.get("line")
        if isinstance(line_value, bool) or not isinstance(line_value, int):
            raise SupplyChainConfigError(
                f"allow entry for {path_value!r}/{pattern_id!r} must define an "
                "integer 'line' (the line of the specific finding to suppress)"
            )
        if line_value < 1:
            raise SupplyChainConfigError(
                f"allow entry for {path_value!r}/{pattern_id!r} has a non-positive "
                f"'line' {line_value!r}; lines are 1-indexed"
            )

        # ``sha256`` binds the entry to the exact reviewed content (the
        # ``sha256:`` digest the gate prints for the finding).
        sha_value = entry.get("sha256")
        if not sha_value or not str(sha_value).strip():
            raise SupplyChainConfigError(
                f"allow entry for {path_value!r}:{line_value}/{pattern_id!r} must "
                "include the finding's content 'sha256' (printed by the gate)"
            )
        sha256 = str(sha_value).strip()

        reason = entry.get("reason")
        if not reason or not str(reason).strip():
            raise SupplyChainConfigError(
                f"allow entry for {path_value!r}:{line_value}/{pattern_id!r} "
                "must include a non-empty 'reason'"
            )

        allowlist[(str(path_value), line_value, pattern_id, sha256)] = str(reason)

    return allowlist


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_text(text: str, *, path: str = "<text>") -> list[ScanFinding]:
    """Scan a block of text line-by-line and return every pattern match.

    Lines are 1-indexed. A single line can produce several findings when it
    matches more than one pattern (e.g. a ``curl … | bash`` line trips both
    ``pipe-to-shell`` and ``network-egress``).
    """
    findings: list[ScanFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        digest = content_digest(line)
        for pattern in PATTERNS:
            match = pattern.regex.search(line)
            if match is None:
                continue
            findings.append(
                ScanFinding(
                    pattern_id=pattern.id,
                    band=pattern.band,
                    path=path,
                    line=lineno,
                    snippet=line.strip()[:200],
                    description=pattern.description,
                    digest=digest,
                )
            )
    return findings


def scan_file(path: str | Path, *, rel: str | None = None) -> list[ScanFinding]:
    """Scan a single file; ``rel`` overrides the path recorded on findings.

    Binary or non-UTF-8 files are skipped (they are not config artifacts), so a
    stray asset under a scanned directory never crashes the gate.
    """
    file_path = Path(path)
    label = rel if rel is not None else str(file_path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return scan_text(text, path=label)


def discover_targets(root: str | Path) -> list[Path]:
    """Enumerate the supply-chain scan surface beneath ``root``.

    Covers ``hooks/``, ``skills/``, ``plugins/*/skills/`` (recursively) plus the
    named ``mcp/config.template.json`` and ``settings.json`` files. Returns a
    sorted, de-duplicated list of existing files; skips VCS/build dirs.
    """
    root_path = Path(root)
    seen: set[Path] = set()

    def _walk(directory: Path) -> None:
        if not directory.is_dir():
            return
        for child in directory.rglob("*"):
            if child.is_dir():
                continue
            if any(part in _SKIP_DIR_NAMES for part in child.parts):
                continue
            seen.add(child)

    for name in _SCAN_DIRS:
        _walk(root_path / name)

    for glob in _SCAN_GLOBS:
        for directory in root_path.glob(glob):
            _walk(directory)

    for name in _SCAN_FILES:
        candidate = root_path / name
        if candidate.is_file():
            seen.add(candidate)

    return sorted(seen)


def classify_findings(
    findings: list[ScanFinding],
    *,
    allowlist: dict[tuple[str, int, str, str], str] | None = None,
) -> ScanResult:
    """Reduce a list of findings to a gate verdict, honoring the allowlist.

    Findings whose ``(path, line, pattern_id, digest)`` matches an allowlist
    entry are removed from the gating set (recorded in ``suppressed``) before the
    verdict. Matching on the line keeps suppression per-finding (a different line
    of the same pattern is still gated); matching on the content digest binds it
    to the reviewed text (swapping the line's content re-surfaces the finding).

    - ``BLOCK`` when any remaining finding is BLOCK-band.
    - ``WARN`` when any remaining finding is WARN-band (and none BLOCK).
    - ``CLEAN`` otherwise (no findings, or all suppressed).
    """
    allow = allowlist or {}

    gating: list[ScanFinding] = []
    suppressed: list[ScanFinding] = []
    for finding in findings:
        if (finding.path, finding.line, finding.pattern_id, finding.digest) in allow:
            suppressed.append(finding)
        else:
            gating.append(finding)

    counts = dict(Counter(f.band for f in gating))

    if any(f.band == "BLOCK" for f in gating):
        status = "BLOCK"
    elif gating:
        status = "WARN"
    else:
        status = "CLEAN"

    return ScanResult(
        status=status,
        findings=gating,
        suppressed=suppressed,
        counts=counts,
    )


def scan_repo(
    root: str | Path,
    *,
    allowlist: dict[tuple[str, int, str, str], str] | None = None,
) -> ScanResult:
    """Discover the scan surface under ``root``, scan it, and classify.

    Findings record the repo-relative path so allowlist entries are written
    against stable, root-independent paths.
    """
    root_path = Path(root)
    findings: list[ScanFinding] = []
    for target in discover_targets(root_path):
        rel = target.relative_to(root_path).as_posix()
        findings.extend(scan_file(target, rel=rel))
    return classify_findings(findings, allowlist=allowlist)
