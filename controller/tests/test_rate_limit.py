# ABOUTME: Unit tests for the pure rate-limit signal detection + wait arithmetic.
# ABOUTME: Story 14.1-003 — detection, reset computation, cap, configured window.

from __future__ import annotations

import pytest

from sdlc.rate_limit import (
    RateLimitSignal,
    WindowQuota,
    detect_rate_limit,
    is_connection_failure,
    seconds_until_reset,
    within_wait_cap,
)


# ---------------------------------------------------------------------------
# detect_rate_limit — recognise a Max throttle / quota signal in agent text
# ---------------------------------------------------------------------------

def test_detect_none_for_unrelated_text() -> None:
    # AC7: no rate-limit signal → None so the caller degrades to today's behaviour.
    assert detect_rate_limit("compilation failed: missing semicolon") is None
    assert detect_rate_limit("") is None
    assert detect_rate_limit(None) is None


def test_detect_429() -> None:
    sig = detect_rate_limit("HTTP 429 Too Many Requests")
    assert sig is not None
    assert sig.source == "429"


def test_detect_rate_limit_phrase() -> None:
    assert detect_rate_limit("Error: rate limit exceeded for this org") is not None
    assert detect_rate_limit("rate_limit_error from the API") is not None
    assert detect_rate_limit("usage limit reached on your plan") is not None


def test_detect_retry_after_captures_seconds() -> None:
    sig = detect_rate_limit("429 rate limited; Retry-After: 120")
    assert sig is not None
    assert sig.source == "retry-after"
    assert sig.retry_after_s == 120


def test_detect_retry_after_json_form() -> None:
    sig = detect_rate_limit('{"type":"error","error":{"retry_after": 45}}')
    assert sig is not None
    assert sig.retry_after_s == 45


def test_detect_reset_at_epoch() -> None:
    sig = detect_rate_limit("anthropic-ratelimit-tokens-reset: 1700000000")
    assert sig is not None
    assert sig.reset_at == 1700000000.0


def test_detect_session_limit_phrase() -> None:
    # Issue #109: the CLI wording for the 5-hour session cap was not matched, so
    # a 429 fell through to a generic dispatch error → false story FAILED.
    sig = detect_rate_limit(
        "You've hit your session limit · resets 8:20pm (Europe/Luxembourg)"
    )
    assert sig is not None
    assert sig.source == "usage-limit"


def test_detect_reset_at_camelcase_resets_at() -> None:
    # Issue #109: the rate_limit_event stream line carries the camelCase
    # ``resetsAt`` epoch key, which the original ``reset[_-]?at`` regex missed.
    sig = detect_rate_limit("rate_limit_info resetsAt:1782066000")
    assert sig is not None
    assert sig.reset_at == 1782066000.0


# ---------------------------------------------------------------------------
# Issue #564: a network outage is NOT a rate limit
# ---------------------------------------------------------------------------

_NETWORK_FAILURES = [
    "Error: getaddrinfo ENOTFOUND api.anthropic.com",
    "connect ECONNREFUSED 160.79.104.10:443",
    "TypeError: fetch failed",
    "curl: (6) Could not resolve host: api.anthropic.com",
    "Temporary failure in name resolution",
    "connect: Network is unreachable",
    "Error: Connection error.",
    "request timed out while contacting the API",
    "you appear to be offline",
]


@pytest.mark.parametrize("text", _NETWORK_FAILURES)
def test_is_connection_failure_recognises_transport_errors(text: str) -> None:
    assert is_connection_failure(text) is True


def test_is_connection_failure_false_for_limit_and_empty_text() -> None:
    assert is_connection_failure("Claude AI usage limit reached") is False
    assert is_connection_failure("") is False
    assert is_connection_failure(None) is False


@pytest.mark.parametrize("text", _NETWORK_FAILURES)
def test_network_failure_is_not_a_rate_limit(text: str) -> None:
    # Issue #564: a dead network says nothing about the plan's quota. Classifying
    # one as a throttle parked the run against the *weekly* reset epoch, and every
    # later resume honoured that epoch — a 30-second wifi drop froze a run for days.
    assert detect_rate_limit(text) is None


def test_network_failure_wins_over_limit_wording() -> None:
    # The CLI wraps a connection error in its own API/limit-flavoured prose. The
    # transport marker is the decisive one: the request never reached the API.
    assert detect_rate_limit(
        "API Error: Connection error. (rate_limit_error?) — fetch failed"
    ) is None
    assert detect_rate_limit(
        "getaddrinfo ENOTFOUND api.anthropic.com; retry_after: 60"
    ) is None


def test_genuine_limit_still_detected_without_transport_marker() -> None:
    # The #564 guard must not blunt #109: a real limit with no transport marker
    # is still a rate-limit signal.
    assert detect_rate_limit("Claude AI usage limit reached. Try again later.") is not None
    assert detect_rate_limit("HTTP 429 Too Many Requests") is not None


# ---------------------------------------------------------------------------
# seconds_until_reset — prefer retry-after, then reset_at, else full window
# ---------------------------------------------------------------------------

def test_seconds_until_reset_prefers_retry_after() -> None:
    sig = RateLimitSignal(source="retry-after", retry_after_s=90, reset_at=999.0)
    assert seconds_until_reset(sig, now=0.0, window_s=18000) == 90


def test_seconds_until_reset_uses_reset_at_when_no_retry_after() -> None:
    sig = RateLimitSignal(source="usage-limit", reset_at=1000.0)
    assert seconds_until_reset(sig, now=400.0, window_s=18000) == 600


def test_seconds_until_reset_never_negative() -> None:
    sig = RateLimitSignal(source="usage-limit", reset_at=100.0)
    assert seconds_until_reset(sig, now=999.0, window_s=18000) == 0


def test_seconds_until_reset_falls_back_to_full_window() -> None:
    # The documented heuristic: no retry-after / reset epoch → assume a full window.
    sig = RateLimitSignal(source="usage-limit")
    assert seconds_until_reset(sig, now=0.0, window_s=18000) == 18000


# ---------------------------------------------------------------------------
# within_wait_cap — the auto-wait vs durable-park boundary
# ---------------------------------------------------------------------------

def test_within_wait_cap_boundary() -> None:
    assert within_wait_cap(300, 18000) is True
    assert within_wait_cap(18000, 18000) is True  # inclusive
    assert within_wait_cap(18001, 18000) is False


# ---------------------------------------------------------------------------
# WindowQuota — configured rolling-window token budget
# ---------------------------------------------------------------------------

def test_window_quota_disabled_when_budget_zero() -> None:
    q = WindowQuota(budget=0, window_s=18000)
    assert q.enabled is False
    assert q.exhausted(10_000_000) is False


def test_window_quota_exhausted_at_budget() -> None:
    q = WindowQuota(budget=1000, window_s=18000, baseline=0)
    assert q.exhausted(999) is False
    assert q.exhausted(1000) is True


def test_window_quota_threshold_pauses_near_limit() -> None:
    q = WindowQuota(budget=1000, window_s=18000, threshold=0.8, baseline=0)
    assert q.exhausted(799) is False
    assert q.exhausted(800) is True


def test_window_quota_used_is_relative_to_baseline() -> None:
    q = WindowQuota(budget=1000, window_s=18000, baseline=5000)
    assert q.used(5400) == 400
    assert q.exhausted(5999) is False
    assert q.exhausted(6000) is True


def test_window_quota_signal_reset_is_start_plus_window() -> None:
    q = WindowQuota(budget=1000, window_s=18000, start=1000.0)
    sig = q.signal()
    assert sig.source == "window-budget"
    assert sig.reset_at == 19000.0


def test_window_quota_reopen_resets_start_and_baseline() -> None:
    q = WindowQuota(budget=1000, window_s=18000, start=0.0, baseline=0)
    q.reopen(now=20000.0, total_tokens=1500)
    assert q.start == 20000.0
    assert q.baseline == 1500
    # After reopening, spend within the *new* window starts from zero again.
    assert q.exhausted(1500) is False
    assert q.exhausted(2500) is True
