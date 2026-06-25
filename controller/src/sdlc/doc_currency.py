# ABOUTME: Documentation-currency lens — flags behavior-changing diffs that ship with no doc update.
# ABOUTME: Story 18.3-001 — a review dimension + build-prompt instruction keep user-facing docs current.

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Config — disable switch + policy
# ---------------------------------------------------------------------------

# Disable switch. The feature ships ON; setting this to a falsey value reverts to
# today's behaviour (no docs instruction in the build prompt, no review
# dimension). Mirrors the ``SDLC_SANDBOX`` env-flag convention.
DOC_CURRENCY_ENV = "SDLC_DOC_CURRENCY"
# Policy selector: ``advisory`` (default, never blocks shipping) or
# ``route_to_bugfix`` (hand a gap to the bounded bugfix loop).
DOC_CURRENCY_POLICY_ENV = "SDLC_DOC_CURRENCY_POLICY"

_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def doc_currency_enabled() -> bool:
    """True unless explicitly disabled via ``$SDLC_DOC_CURRENCY`` (0/false/no/off).

    Default ON: the feature is the shipped behaviour. Only the explicit off
    values flip it back to today's pipeline (AC: disabled ⇒ unchanged behaviour).
    """
    return os.environ.get(DOC_CURRENCY_ENV, "").strip().lower() not in _OFF_VALUES


class Policy(str, Enum):
    """What the controller does with documentation-currency findings."""

    ADVISORY = "advisory"  # record on the PR; never blocks shipping (default)
    ROUTE_TO_BUGFIX = "route_to_bugfix"  # hand the gap to the bounded bugfix loop


DEFAULT_POLICY = Policy.ADVISORY


def policy_from_env() -> Policy:
    """Resolve the active policy from ``$SDLC_DOC_CURRENCY_POLICY``.

    Any unrecognised value falls back to :data:`DEFAULT_POLICY` so a typo never
    silently escalates a gap into the bugfix loop.
    """
    raw = os.environ.get(DOC_CURRENCY_POLICY_ENV, "").strip().lower()
    try:
        return Policy(raw)
    except ValueError:
        return DEFAULT_POLICY


# ---------------------------------------------------------------------------
# Path classification — conservative, to hold the false-positive rate down
# ---------------------------------------------------------------------------

# A behaviour category, the path predicate that detects it, the doc a reviewer
# should look at, and the one-line "why" surfaced in a finding. Order matters:
# the first matching category wins, so the most specific predicates come first.
@dataclass(frozen=True)
class _Category:
    name: str
    matches: object  # Callable[[str], bool]; typed loosely to avoid an import
    doc_hint: str
    reason: str


def _under(path: str, *segments: str) -> bool:
    """True when any ``segment`` appears as a path component of ``path``."""
    parts = path.split("/")
    return any(seg in parts for seg in segments)


# Test/fixture files are never user-facing behaviour, regardless of where they
# live (a skill or hook can ship its own tests). Excluding them first keeps the
# heuristic quiet on test-only churn.
_TEST_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")


def _is_test(path: str) -> bool:
    return bool(_TEST_RE.search(path)) or _under(path, "tests")


_CATEGORIES: tuple[_Category, ...] = (
    _Category(
        name="cli",
        matches=lambda p: p.endswith("controller/src/sdlc/cli.py"),
        doc_hint="README.md / docs/controller-architecture.md (CLI reference)",
        reason="a CLI verb/flag definition changed but no user-facing doc was updated",
    ),
    _Category(
        name="skill",
        matches=lambda p: _under(p, "skills"),
        doc_hint="the skill's SKILL.md / README.md",
        reason="a skill changed but no user-facing doc was updated",
    ),
    _Category(
        name="hook",
        matches=lambda p: _under(p, "hooks"),
        doc_hint="docs/ (hooks documentation) / README.md",
        reason="a hook changed but no user-facing doc was updated",
    ),
    _Category(
        name="installer",
        matches=lambda p: (
            p in {"setup.sh", "bootstrap.sh", "bootstrap-dist.sh"}
            or _under(p, "lib")
            and p.endswith(".sh")
        ),
        doc_hint="README.md (installation steps)",
        reason="an installer step changed but no user-facing doc was updated",
    ),
)


def is_behavior_changing(path: str) -> str | None:
    """Return the behaviour category a path belongs to, or ``None`` if neutral.

    Conservative on purpose: only the touch-points the story names (CLI verbs,
    skills, hooks, installer steps) count, and test/fixture files never do.
    """
    if _is_test(path):
        return None
    for category in _CATEGORIES:
        if category.matches(path):  # type: ignore[operator]
            return category.name
    return None


# CHANGELOG is owned by the Epic-05 release workflow — it never counts as a
# user-facing doc here, so a CHANGELOG-only bump neither satisfies doc currency
# nor is ever flagged as the stale doc.
_CHANGELOG_RE = re.compile(r"(^|/)CHANGELOG(\.[A-Za-z0-9]+)?$", re.IGNORECASE)


def is_doc(path: str) -> bool:
    """True when a path is a user-facing doc whose update keeps behaviour current.

    Markdown anywhere (``README.md``, ``docs/**``, a skill's ``SKILL.md``)
    counts; the CHANGELOG is explicitly excluded (Epic-05 owns it).
    """
    if _CHANGELOG_RE.search(path):
        return False
    return path.endswith(".md") or _under(path, "docs")


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_RENAME_RE = re.compile(r"^rename (?:from|to) (.+)$")
_PLUSMINUS_RE = re.compile(r"^[+-]{3} [ab]/(.+)$")


def paths_from_diff(diff: str) -> list[str]:
    """Collect the repo-relative paths a unified ``git diff`` touches.

    Both sides of a rename are surfaced (so a doc rename still reads as a doc
    touch); ``/dev/null`` is dropped. Order-preserving, de-duplicated.
    """
    seen: list[str] = []
    for line in diff.splitlines():
        path = None
        if m := _DIFF_GIT_RE.match(line):
            for candidate in (m.group(1), m.group(2)):
                if candidate not in seen:
                    seen.append(candidate)
            continue
        if m := _RENAME_RE.match(line):
            path = m.group(1)
        elif m := _PLUSMINUS_RE.match(line):
            path = m.group(1)
        if path and path != "/dev/null" and path not in seen:
            seen.append(path)
    return seen


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocCurrencyFinding:
    """One stale-doc finding: which doc looks stale and the one-line why."""

    category: str
    source_path: str
    doc_hint: str
    reason: str


@dataclass(frozen=True)
class DocCurrencyResult:
    """The lens's verdict on one diff."""

    enabled: bool
    policy: Policy
    findings: list[DocCurrencyFinding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def route_to_bugfix(self) -> bool:
        """True only when the policy routes *and* there is something to route."""
        return self.policy is Policy.ROUTE_TO_BUGFIX and self.has_findings

    def summary(self) -> str:
        """A short, human-readable one-liner for logs / advisory PR comments."""
        if not self.enabled:
            return "documentation-currency lens disabled"
        if not self.findings:
            return "documentation-currency: docs look current"
        cats = ", ".join(sorted({f.category for f in self.findings}))
        return (
            f"documentation-currency: {len(self.findings)} stale-doc "
            f"finding(s) [{cats}] (policy={self.policy.value})"
        )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_paths(
    paths: list[str],
    *,
    enabled: bool | None = None,
    policy: Policy | None = None,
) -> DocCurrencyResult:
    """Score a set of touched paths for documentation currency.

    Emits a finding per distinct behaviour category **only** when the diff
    changes user-facing behaviour and ships *no* doc update. Any doc touch in the
    same diff suppresses all findings — the conservative, low-false-positive
    choice. ``enabled``/``policy`` default to the env-resolved values.
    """
    is_enabled = doc_currency_enabled() if enabled is None else enabled
    active_policy = policy_from_env() if policy is None else policy

    if not is_enabled:
        return DocCurrencyResult(enabled=False, policy=active_policy, findings=[])

    behavior: list[tuple[str, str]] = []
    has_doc = False
    for path in paths:
        if is_doc(path):
            has_doc = True
        category = is_behavior_changing(path)
        if category is not None:
            behavior.append((path, category))

    if not behavior or has_doc:
        # Docs-only, behaviour-neutral, or docs already updated alongside.
        return DocCurrencyResult(enabled=True, policy=active_policy, findings=[])

    by_name = {c.name: c for c in _CATEGORIES}
    findings: list[DocCurrencyFinding] = []
    seen: set[str] = set()
    for path, name in behavior:
        if name in seen:
            continue
        seen.add(name)
        meta = by_name[name]
        findings.append(
            DocCurrencyFinding(
                category=name,
                source_path=path,
                doc_hint=meta.doc_hint,
                reason=meta.reason,
            )
        )
    return DocCurrencyResult(enabled=True, policy=active_policy, findings=findings)


def analyze_diff(
    diff: str,
    *,
    enabled: bool | None = None,
    policy: Policy | None = None,
) -> DocCurrencyResult:
    """Score a unified ``git diff`` for documentation currency."""
    return analyze_paths(paths_from_diff(diff), enabled=enabled, policy=policy)
