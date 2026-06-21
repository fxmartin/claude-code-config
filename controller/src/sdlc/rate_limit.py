# ABOUTME: Pure rate-limit / quota signal detection + wait-window arithmetic (Story 14.1-003).
# ABOUTME: No I/O — dispatch.py raises on the detected signal; build.py decides wait-vs-park.

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "RateLimitSignal",
    "detect_rate_limit",
    "seconds_until_reset",
    "within_wait_cap",
    "WindowQuota",
]

# Patterns that mark a Max-plan rate-limit / quota-exhaustion in an agent's
# stderr or result text. The controller has zero rate-limit handling today: a
# limit hit surfaces as a non-zero ``claude -p`` exit whose stderr names the
# cause. These are matched case-insensitively against that text so a throttle is
# recognised as a recoverable pause instead of a generic dispatch failure.
_RATE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brate[\s_-]?limit", re.I),
    re.compile(r"rate_limit_error", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"\busage limit reached\b", re.I),
    re.compile(r"\bquota (?:exceeded|exhausted|reached)\b", re.I),
    re.compile(r"\btoo many requests\b", re.I),
    # Issue #109: the claude CLI's 5-hour session cap wording ("You've hit your
    # session limit · resets 8:20pm"). No explicit retry-after → "usage-limit".
    re.compile(r"\bsession limit\b", re.I),
    re.compile(r"\bhit your .*\blimit\b", re.I),
)

# Explicit backoff hint the API surfaces a few ways: an HTTP ``Retry-After: 120``
# header, a ``retry_after=120`` field, or a ``"retry_after": 120`` JSON pair. The
# captured integer is honoured as a *short* backoff (AC: a hard 429 mid-stage is
# a short recoverable pause, never a stage FAILED).
_RETRY_AFTER = re.compile(r"retry[\s_-]?after['\"]?\s*[:=]\s*['\"]?(\d+)", re.I)

# Some surfaces carry an absolute window-reset epoch (e.g. the
# ``anthropic-ratelimit-*-reset`` family, or the CLI's camelCase ``resetsAt``
# from a rate_limit_event stream line — issue #109). When present it is preferred
# over a relative retry-after because it survives clock skew between dispatch and
# wait. The optional ``s`` matches both ``reset_at`` and ``resetsAt``.
_RESET_AT = re.compile(r"(?:ratelimit[\w-]*reset|reset(?:s)?[\s_-]?at)['\"]?\s*[:=]\s*['\"]?(\d+)", re.I)


@dataclass(frozen=True)
class RateLimitSignal:
    """A detected (or configured) rate-limit / quota-exhaustion event.

    ``source`` names where the signal came from (``"429"`` / ``"retry-after"`` /
    ``"usage-limit"`` / ``"window-budget"``) for the audit trail. ``retry_after_s``
    is an explicit relative backoff in seconds when the agent surfaced one;
    ``reset_at`` is an absolute epoch when the window reopens when that was
    surfaced instead. Both may be ``None`` — then the wait falls back to a full
    rolling window (the documented heuristic; see :func:`seconds_until_reset`).
    """

    source: str
    retry_after_s: int | None = None
    reset_at: float | None = None


def detect_rate_limit(text: str | None) -> RateLimitSignal | None:
    """Classify agent stderr/result ``text`` as a rate-limit signal, or ``None``.

    Returns a :class:`RateLimitSignal` when ``text`` names a throttle / quota
    exhaustion (or carries an explicit ``retry-after``), else ``None`` so the
    caller treats the failure as today's generic dispatch error (graceful
    degradation when no rate-limit signal is present — AC7).
    """
    if not text:
        return None

    retry_after: int | None = None
    m = _RETRY_AFTER.search(text)
    if m:
        retry_after = int(m.group(1))

    reset_at: float | None = None
    r = _RESET_AT.search(text)
    if r:
        reset_at = float(r.group(1))

    matched = any(p.search(text) for p in _RATE_LIMIT_PATTERNS)
    if not matched and retry_after is None and reset_at is None:
        return None

    if retry_after is not None:
        source = "retry-after"
    elif "429" in text:
        source = "429"
    else:
        source = "usage-limit"
    return RateLimitSignal(source=source, retry_after_s=retry_after, reset_at=reset_at)


def seconds_until_reset(signal: RateLimitSignal, *, now: float, window_s: int) -> int:
    """Seconds the controller should wait before the window reopens.

    Preference order: an explicit ``retry_after_s`` (a hard 429's short backoff),
    then an absolute ``reset_at`` epoch (``reset_at - now``), else the configured
    rolling ``window_s``. Window reset times are approximate — when neither a
    retry-after nor a reset epoch is surfaced, the heuristic assumes a *full*
    window has to elapse (conservative: never resume early into a still-closed
    window). Never negative.
    """
    if signal.retry_after_s is not None:
        return max(0, signal.retry_after_s)
    if signal.reset_at is not None:
        return max(0, int(round(signal.reset_at - now)))
    return max(0, int(window_s))


def within_wait_cap(wait_s: int, max_wait_s: int) -> bool:
    """Whether ``wait_s`` is within the configurable in-process auto-wait cap.

    At/under the cap the controller waits in-process and auto-resumes the same
    run; beyond it (e.g. a weekly cap days away) it durably parks for a later
    ``sdlc resume`` rather than holding the process indefinitely.
    """
    return wait_s <= max_wait_s


@dataclass
class WindowQuota:
    """A configured rolling-window token budget tracked from the 11.1-003 accrual.

    The proactive counterpart to the reactive 429 signal: when no rate-limit
    header is available, a configured per-window token budget gates dispatch.
    ``baseline`` is the run's accrued tokens at the window's start, so usage
    *within* the current window is ``total - baseline``. The window is exhausted
    once that reaches ``budget * threshold`` (``threshold`` < 1 pauses *near* the
    limit). Reset times are approximate — the window is assumed to reopen
    ``window_s`` after it opened (``start``).
    """

    budget: int
    window_s: int
    threshold: float = 1.0
    start: float = 0.0
    baseline: int = 0

    @property
    def enabled(self) -> bool:
        """A budget of 0 means "no configured window quota" (never gates)."""
        return self.budget > 0

    def used(self, total_tokens: int) -> int:
        """Tokens spent within the current window (never negative)."""
        return max(0, total_tokens - self.baseline)

    def exhausted(self, total_tokens: int) -> bool:
        """Whether the window's usage has reached the (thresholded) budget."""
        if not self.enabled:
            return False
        return self.used(total_tokens) >= self.budget * self.threshold

    def signal(self) -> RateLimitSignal:
        """The pause signal for an exhausted window: reset is ``start + window_s``."""
        return RateLimitSignal(source="window-budget", reset_at=self.start + self.window_s)

    def reopen(self, now: float, total_tokens: int) -> None:
        """Reopen the window after a wait: new start = ``now``, baseline = current total."""
        self.start = now
        self.baseline = total_tokens
