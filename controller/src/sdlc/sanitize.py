# ABOUTME: Sanitizes untrusted text before it is embedded in a privileged-agent prompt.
# ABOUTME: Story 13.3-001 — strips zero-width/bidi Unicode, HTML comment/script, data/base64.

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

# The controller dispatches every agent under ``--dangerously-skip-permissions``
# (``dispatch.py``), so any text embedded in the prompt is read by an agent with
# no permission prompt between it and the host. Untrusted text (story bodies,
# issue/PR comments) is therefore a prompt-injection surface: a hidden
# instruction in invisible Unicode, an HTML comment, a ``<script>`` block, or a
# ``data:``/``base64,`` payload could hijack the agent. This module neutralizes
# those vectors at the dispatch boundary and keeps a structured record of what it
# stripped, so a suspicious payload is logged (and, above a threshold, can be
# routed to human review) rather than silently obeyed.

logger = logging.getLogger("sdlc.sanitize")

# Finding categories. Stable string ids so log consumers and the security
# reference can key off them.
CATEGORY_ZERO_WIDTH = "zero-width-unicode"
CATEGORY_HTML_COMMENT = "html-comment"
CATEGORY_SCRIPT = "script-tag"
CATEGORY_DATA_URI = "data-uri"
CATEGORY_BASE64 = "base64-payload"

# Per-category risk weight. A single deliberate-injection vector (script, HTML
# comment, data: URI) is high signal; a lone zero-width char is lower (it can be
# an accidental paste artefact), so it is stripped and logged but does not on its
# own cross the review threshold.
_SEVERITY: dict[str, int] = {
    CATEGORY_ZERO_WIDTH: 1,
    CATEGORY_BASE64: 2,
    CATEGORY_DATA_URI: 3,
    CATEGORY_HTML_COMMENT: 3,
    CATEGORY_SCRIPT: 5,
}

# A weighted-risk score at or above this routes the payload to human review.
# Tuned so any single script/HTML-comment/data-URI vector trips it, a lone
# zero-width char does not, but a cluster of them does.
DEFAULT_REVIEW_THRESHOLD = 3

# Placeholder left in place of a neutralized (not deleted) payload, so the agent
# sees an inert marker and a reviewer can tell something was removed.
_DATA_URI_PLACEHOLDER = "[sanitized:data-uri]"
_BASE64_PLACEHOLDER = "[sanitized:base64]"

# Zero-width and bidirectional control characters. None of these are legitimate
# in source code, markdown, or prose — they are the classic invisible-injection
# vector — so they are stripped everywhere, including inside code fences.
_ZERO_WIDTH_CHARS = (
    "​"  # ZERO WIDTH SPACE
    "‌"  # ZERO WIDTH NON-JOINER
    "‍"  # ZERO WIDTH JOINER
    "‎"  # LEFT-TO-RIGHT MARK
    "‏"  # RIGHT-TO-LEFT MARK
    "‪"  # LEFT-TO-RIGHT EMBEDDING
    "‫"  # RIGHT-TO-LEFT EMBEDDING
    "‬"  # POP DIRECTIONAL FORMATTING
    "‭"  # LEFT-TO-RIGHT OVERRIDE
    "‮"  # RIGHT-TO-LEFT OVERRIDE
    "⁠"  # WORD JOINER
    "⁡"  # FUNCTION APPLICATION
    "⁢"  # INVISIBLE TIMES
    "⁣"  # INVISIBLE SEPARATOR
    "⁤"  # INVISIBLE PLUS
    "⁦"  # LEFT-TO-RIGHT ISOLATE
    "⁧"  # RIGHT-TO-LEFT ISOLATE
    "⁨"  # FIRST STRONG ISOLATE
    "⁩"  # POP DIRECTIONAL ISOLATE
    "﻿"  # ZERO WIDTH NO-BREAK SPACE / BOM
)
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH_CHARS}]")

# HTML comments may hide multi-line instructions, so match across newlines.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# ``<script>…</script>`` blocks (and stray open/close tags) — case-insensitive,
# across newlines.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_TAG_RE = re.compile(r"</?script\b[^>]*>", re.IGNORECASE)
# ``data:`` URIs — captures ``data:text/html,…`` and ``data:…;base64,…`` up to
# the first whitespace/closing delimiter. The base64 payload it carries is part
# of this one finding (it is not double-counted by the base64 pass below).
_DATA_URI_RE = re.compile(r"data:[\w./+-]*(?:;[\w=+-]+)*,[^\s)>\]\"']*", re.IGNORECASE)
# A standalone ``base64,`` payload marker that is not part of a ``data:`` URI.
_BASE64_RE = re.compile(r";?base64,[A-Za-z0-9+/=]*", re.IGNORECASE)

# Fenced code blocks (``` or ~~~). Inside a fence only the always-unsafe
# zero-width strip applies, so a story legitimately quoting ``<script>`` or a
# ``data:`` URI as a code sample survives intact (the technical note's
# "conservative escaping for code fences"). An *unterminated* fence does not
# match, so it falls through to full sanitization — the safe default.
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)


@dataclass(frozen=True)
class SanitizationFinding:
    """One class of dangerous content that was stripped or neutralized.

    ``category`` is one of the ``CATEGORY_*`` ids, ``count`` how many instances
    were found, and ``action`` either ``"stripped"`` (removed) or
    ``"neutralized"`` (replaced by an inert placeholder).
    """

    category: str
    count: int
    action: str


@dataclass(frozen=True)
class SanitizationResult:
    """The outcome of sanitizing one untrusted string.

    ``cleaned`` is the safe text to embed in the prompt; ``findings`` records what
    was removed (empty when the input was clean, so clean text round-trips).
    """

    cleaned: str
    findings: list[SanitizationFinding]

    @property
    def modified(self) -> bool:
        """True when at least one dangerous pattern was stripped/neutralized."""
        return bool(self.findings)

    @property
    def total(self) -> int:
        """Total number of dangerous instances across all categories."""
        return sum(f.count for f in self.findings)

    @property
    def risk_score(self) -> int:
        """Severity-weighted score used for the review-routing decision."""
        return sum(_SEVERITY.get(f.category, 1) * f.count for f in self.findings)

    @property
    def categories(self) -> dict[str, int]:
        """Per-category instance counts (``{category: count}``)."""
        return {f.category: f.count for f in self.findings}

    def review_recommended(self, threshold: int = DEFAULT_REVIEW_THRESHOLD) -> bool:
        """True when the weighted risk meets ``threshold`` — gate to human review."""
        return self.risk_score >= threshold


def _strip_zero_width(text: str) -> tuple[str, int]:
    """Remove every zero-width/bidi control char; return (cleaned, count)."""
    count = len(_ZERO_WIDTH_RE.findall(text))
    return (_ZERO_WIDTH_RE.sub("", text) if count else text), count


def _sub_count(pattern: re.Pattern[str], repl: str, text: str) -> tuple[str, int]:
    """``pattern.subn`` returning (cleaned, n) — a thin, readable wrapper."""
    return pattern.subn(repl, text)


def _sanitize_outside_code(text: str, counts: Counter[str]) -> str:
    """Apply every transformation to a non-code segment, tallying into ``counts``.

    Order matters: HTML comments and ``<script>`` blocks are removed first (they
    may themselves contain ``data:``/``base64`` payloads), then ``data:`` URIs
    (which absorb any base64 they carry), then standalone base64 markers, and
    finally the always-safe zero-width strip.
    """
    text, n = _sub_count(_HTML_COMMENT_RE, "", text)
    counts[CATEGORY_HTML_COMMENT] += n

    text, n = _sub_count(_SCRIPT_RE, "", text)
    text, n2 = _sub_count(_SCRIPT_TAG_RE, "", text)
    counts[CATEGORY_SCRIPT] += n + n2

    text, n = _sub_count(_DATA_URI_RE, _DATA_URI_PLACEHOLDER, text)
    counts[CATEGORY_DATA_URI] += n

    text, n = _sub_count(_BASE64_RE, _BASE64_PLACEHOLDER, text)
    counts[CATEGORY_BASE64] += n

    text, n = _strip_zero_width(text)
    counts[CATEGORY_ZERO_WIDTH] += n
    return text


def sanitize_untrusted(text: str) -> SanitizationResult:
    """Strip/neutralize dangerous patterns in ``text`` and report what was removed.

    Splits the text into fenced code blocks and the prose around them. Inside a
    code fence only the always-unsafe zero-width/bidi characters are stripped, so
    legitimate code samples (which may quote ``<script>`` or a ``data:`` URI)
    survive. Everywhere else, HTML comments, ``<script>`` blocks, ``data:`` URIs,
    and ``base64,`` payloads are removed/neutralized too. Clean text produces an
    empty ``findings`` list and an unchanged ``cleaned`` string.
    """
    counts: Counter[str] = Counter()
    out: list[str] = []
    last = 0
    for match in _FENCE_RE.finditer(text):
        # Prose before this fence: full sanitization.
        out.append(_sanitize_outside_code(text[last : match.start()], counts))
        # The fenced block itself: only the invisible-Unicode strip.
        fenced, n = _strip_zero_width(match.group(0))
        counts[CATEGORY_ZERO_WIDTH] += n
        out.append(fenced)
        last = match.end()
    # Trailing prose after the last fence (or the whole string when no fence).
    out.append(_sanitize_outside_code(text[last:], counts))

    findings = [
        SanitizationFinding(
            category=category,
            count=count,
            action="neutralized"
            if category in (CATEGORY_DATA_URI, CATEGORY_BASE64)
            else "stripped",
        )
        # Stable, severity-descending order for readable logs.
        for category, count in sorted(
            counts.items(), key=lambda kv: (-_SEVERITY.get(kv[0], 1), kv[0])
        )
        if count
    ]
    return SanitizationResult(cleaned="".join(out), findings=findings)


def sanitize_prompt(
    text: str,
    *,
    source: str,
    threshold: int = DEFAULT_REVIEW_THRESHOLD,
) -> SanitizationResult:
    """Sanitize untrusted ``text`` at the dispatch boundary and log any action.

    ``source`` labels where the text is bound (e.g. the agent/stage name) for the
    audit log. Emits a structured WARNING when something was stripped — escalated
    to flag ``review_recommended`` once the weighted risk crosses ``threshold`` —
    and stays silent for clean text, so a normal run logs nothing. Returns the
    :class:`SanitizationResult`; callers embed ``result.cleaned`` in the prompt.
    """
    result = sanitize_untrusted(text)
    if result.modified:
        review = result.review_recommended(threshold)
        logger.warning(
            "sanitized untrusted input source=%s categories=%s "
            "risk_score=%d review_recommended=%s",
            source,
            result.categories,
            result.risk_score,
            review,
        )
    return result
