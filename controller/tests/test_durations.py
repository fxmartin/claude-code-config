# ABOUTME: Tests for the run/story duration helpers (Story 11.2-005).
# ABOUTME: Pure-logic — parses ledger timestamps and computes human-ready spans.

from __future__ import annotations

from datetime import datetime, timezone

from sdlc.build import _duration_seconds, _parse_ts, _story_duration_seconds

# A fixed "now" so elapsed computations are deterministic in tests.
_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_ts_handles_sqlite_space_separator() -> None:
    dt = _parse_ts("2026-06-20 11:30:00")
    assert dt == datetime(2026, 6, 20, 11, 30, 0, tzinfo=timezone.utc)


def test_parse_ts_handles_iso_t_separator() -> None:
    dt = _parse_ts("2026-06-20T11:30:00")
    assert dt == datetime(2026, 6, 20, 11, 30, 0, tzinfo=timezone.utc)


def test_parse_ts_returns_none_for_empty_or_garbage() -> None:
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


def test_duration_seconds_finished_run() -> None:
    secs = _duration_seconds("2026-06-20 11:00:00", "2026-06-20 11:04:12")
    assert secs == 252  # 4m 12s


def test_duration_seconds_in_progress_uses_now() -> None:
    # finished_at is None → elapsed from started_at to `now`.
    secs = _duration_seconds("2026-06-20 11:30:00", None, now=_NOW)
    assert secs == 1800  # 30m


def test_duration_seconds_missing_start_is_none() -> None:
    assert _duration_seconds(None, "2026-06-20 11:04:12") is None
    assert _duration_seconds("", None, now=_NOW) is None


def test_duration_seconds_negative_span_degrades_to_none() -> None:
    # finished before started (clock skew / bad data) → never negative.
    assert _duration_seconds("2026-06-20 11:04:12", "2026-06-20 11:00:00") is None


def test_story_duration_spans_earliest_start_to_latest_finish() -> None:
    stages = [
        {"name": "build", "started_at": "2026-06-20 11:00:00", "finished_at": "2026-06-20 11:02:00"},
        {"name": "review", "started_at": "2026-06-20 11:03:00", "finished_at": "2026-06-20 11:05:30"},
    ]
    assert _story_duration_seconds(stages) == 330  # 11:00:00 → 11:05:30


def test_story_duration_in_flight_uses_now() -> None:
    stages = [
        {"name": "build", "started_at": "2026-06-20 11:00:00", "finished_at": "2026-06-20 11:02:00"},
        {"name": "review", "started_at": "2026-06-20 11:03:00", "finished_at": None},
    ]
    # An unfinished stage → elapsed-so-far from earliest start to `now`.
    assert _story_duration_seconds(stages, now=_NOW) == 3600  # 11:00:00 → 12:00:00


def test_story_duration_no_started_stages_is_none() -> None:
    stages = [
        {"name": "build", "status": "PENDING", "started_at": None, "finished_at": None},
    ]
    assert _story_duration_seconds(stages, now=_NOW) is None


def test_story_duration_empty_is_none() -> None:
    assert _story_duration_seconds([], now=_NOW) is None
