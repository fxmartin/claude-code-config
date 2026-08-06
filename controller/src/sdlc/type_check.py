# ABOUTME: Static type-check ratchet — parses mypy output and gates on new violations only.
# ABOUTME: Issue #586 — lets a 27k-line backlog drain gradually while blocking every new type error.

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Bumped only when the on-disk baseline shape changes incompatibly. An older
# controller reading a newer baseline must fail loudly rather than silently
# treating unknown entries as "no known violations" and green-lighting the gate.
BASELINE_VERSION = 1

# The baseline lives next to the checker config it belongs to.
DEFAULT_BASELINE_PATH = Path(".mypy-baseline.json")

# `path:line: severity: message  [code]`, tolerating mypy's optional column and
# --show-error-end forms (`path:line:col:` and `path:line:col:endline:endcol:`).
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::\d+){0,3}: "
    r"(?P<severity>error|warning|note): "
    r"(?P<rest>.*)$"
)

# Trailing `  [error-code]`, which mypy omits for a few diagnostics (syntax
# errors, some internal reports).
_CODE_RE = re.compile(r"^(?P<message>.*?)\s+\[(?P<code>[a-z][a-z0-9-]*)\]$")

# Messages that quote a *source line number* ("already defined on line 12")
# would otherwise churn the baseline on every unrelated edit above the target.
_LINE_REF_RE = re.compile(r"\bline \d+\b")

# Only genuine errors gate; `note:` lines are explanatory continuations of an
# error that is already counted.
_GATING_SEVERITY = "error"

# Signature field separator. Mypy paths and error codes never contain "|", and
# the message is the last field, so a "|" inside it cannot shift the split.
_SIGNATURE_SEP = "|"


class TypeCheckError(Exception):
    """Base error for the type-check ratchet."""


class TypeCheckBaselineError(TypeCheckError):
    """A committed baseline file was missing required structure or was malformed."""


@dataclass(frozen=True)
class MypyDiagnostic:
    """One mypy diagnostic, normalized to the fields the ratchet keys on."""

    path: str
    line: int
    severity: str
    message: str
    code: str

    @property
    def signature(self) -> str:
        """The line-number-independent identity used to match against a baseline.

        Deliberately excludes :attr:`line`: a baselined violation must survive
        unrelated edits that shift it up or down the file, or the gate would
        re-fail on every neighbouring change.
        """
        normalized = _LINE_REF_RE.sub("line N", self.message)
        return _SIGNATURE_SEP.join((self.path, self.code, normalized))

    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class Baseline:
    """The accepted backlog: how many times each violation signature may occur."""

    violations: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.violations.values())


@dataclass(frozen=True)
class RatchetResult:
    """The verdict of one ratchet comparison.

    ``BLOCK`` when the run introduced violations beyond the baseline, ``WARN``
    when the baseline is stale (violations were fixed but not pruned), and
    ``CLEAN`` when the run matches the baseline exactly.
    """

    status: str
    new_violations: list[MypyDiagnostic]
    fixed: dict[str, int]
    total: int
    baseline_total: int

    @property
    def fixed_total(self) -> int:
        return sum(self.fixed.values())


def parse_mypy_output(text: str) -> list[MypyDiagnostic]:
    """Parse mypy's default text output into gating diagnostics.

    Non-diagnostic chatter (the ``Found N errors`` summary, ``Success:`` line,
    blank lines) and ``note:`` continuations are dropped; only ``error:`` lines
    reach the ratchet.
    """
    diagnostics: list[MypyDiagnostic] = []
    for raw_line in text.splitlines():
        match = _DIAGNOSTIC_RE.match(raw_line.rstrip())
        if match is None:
            continue
        if match["severity"] != _GATING_SEVERITY:
            continue

        rest = match["rest"]
        code_match = _CODE_RE.match(rest)
        message = code_match["message"] if code_match else rest
        code = code_match["code"] if code_match else ""

        diagnostics.append(
            MypyDiagnostic(
                path=match["path"],
                line=int(match["line"]),
                severity=match["severity"],
                message=message,
                code=code,
            )
        )
    return diagnostics


def build_baseline(diagnostics: list[MypyDiagnostic]) -> Baseline:
    """Snapshot the current violations as the accepted backlog."""
    return Baseline(dict(Counter(d.signature for d in diagnostics)))


def render_baseline(baseline: Baseline) -> str:
    """Serialize a baseline to its committed JSON form.

    Keys are sorted and the file is newline-terminated so regenerating it
    produces a reviewable diff rather than a reshuffle.
    """
    payload = {
        "version": BASELINE_VERSION,
        "violations": dict(sorted(baseline.violations.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def load_baseline(path: str | Path) -> Baseline:
    """Load a committed baseline; an absent file means "no accepted backlog".

    A missing file is the legitimate first-run state, so it yields an empty
    baseline (every violation is new). Anything present but unreadable raises
    :class:`TypeCheckBaselineError` — a corrupt baseline must never be silently
    downgraded to "empty", which would flip the gate from strict to strictest
    or, worse, mask the fact that the ratchet stopped working.
    """
    baseline_path = Path(path)
    if not baseline_path.is_file():
        return Baseline({})

    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TypeCheckBaselineError(
            f"{baseline_path} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise TypeCheckBaselineError(
            f"{baseline_path} must be a JSON object, got {type(data).__name__}."
        )

    version = data.get("version")
    if version != BASELINE_VERSION:
        raise TypeCheckBaselineError(
            f"{baseline_path} has unsupported version {version!r}; "
            f"this controller writes version {BASELINE_VERSION}."
        )

    raw_violations: Any = data.get("violations")
    if not isinstance(raw_violations, dict):
        raise TypeCheckBaselineError(
            f"{baseline_path} must map 'violations' to an object of "
            "signature -> count."
        )

    violations: dict[str, int] = {}
    for signature, count in raw_violations.items():
        # bool is an int subclass; `True` as a count is a corrupt baseline.
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise TypeCheckBaselineError(
                f"{baseline_path}: count for {signature!r} must be a positive "
                f"integer, got {count!r}."
            )
        violations[str(signature)] = count

    return Baseline(violations)


def compare_to_baseline(
    diagnostics: list[MypyDiagnostic], baseline: Baseline
) -> RatchetResult:
    """Gate a mypy run against its accepted backlog.

    Matching is per-signature *with counts*, so duplicating a baselined
    violation still counts as new. Fixing one never fails the build; it only
    reports ``WARN`` so the baseline gets pruned and the ratchet tightens.
    """
    current = Counter(d.signature for d in diagnostics)

    new_violations: list[MypyDiagnostic] = []
    for signature, count in current.items():
        excess = count - baseline.violations.get(signature, 0)
        if excess > 0:
            # Report the trailing occurrences: the earlier ones in file order
            # are the plausibly pre-existing pair for the baselined entry.
            matching = [d for d in diagnostics if d.signature == signature]
            new_violations.extend(matching[-excess:])

    fixed = {
        signature: count - current.get(signature, 0)
        for signature, count in baseline.violations.items()
        if count > current.get(signature, 0)
    }

    new_violations.sort(key=lambda d: (d.path, d.line, d.code))

    if new_violations:
        status = "BLOCK"
    elif fixed:
        status = "WARN"
    else:
        status = "CLEAN"

    return RatchetResult(
        status=status,
        new_violations=new_violations,
        fixed=fixed,
        total=sum(current.values()),
        baseline_total=baseline.total,
    )
