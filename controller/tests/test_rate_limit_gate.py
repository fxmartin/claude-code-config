# ABOUTME: Tests for rate-limit / quota awareness + automatic resume (Story 14.1-003).
# ABOUTME: Synthetic 429s drive in-process auto-wait, durable park, and resume.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import BuildOptions, Ledger, parse_build_args, run_build
from sdlc.dispatch import RateLimitError
from sdlc.rate_limit import RateLimitSignal
from sdlc.resume import run_resume

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue


# ---------------------------------------------------------------------------
# Test doubles: a dispatcher that raises a synthetic rate limit on demand
# ---------------------------------------------------------------------------

class RateLimitingDispatcher(FakeDispatcher):
    """A FakeDispatcher that raises RateLimitError a bounded number of times.

    ``trip_on`` is the ``(agent_type, story_id)`` the throttle fires on; after
    ``times`` throttles it falls through to the normal canned success, so a
    within-cap test sees the run resume and complete.
    """

    def __init__(self, *, trip_on, signal, times=1, overrides=None) -> None:
        super().__init__(overrides)
        self.trip_on = trip_on
        self.signal = signal
        self.times = times
        self.tripped = 0

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        sid = getattr(story, "id", "")
        if (agent_type, sid) == self.trip_on and self.tripped < self.times:
            self.tripped += 1
            raise RateLimitError("synthetic throttle", signal=self.signal)
        return super().__call__(agent_type, prompt, story=story, **kwargs)


class _Sleeps:
    """Records sleep durations instead of actually sleeping (instant tests)."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _opts(**kw) -> BuildOptions:
    base = dict(scope="epic-99", skip_preflight=True, sequential=True)
    base.update(kw)
    return BuildOptions(**base)


# ---------------------------------------------------------------------------
# Argument parsing — the new rate-limit flags
# ---------------------------------------------------------------------------

def test_parse_rate_limit_flags() -> None:
    opts = parse_build_args([
        "epic-99",
        "--rate-limit-max-wait=600",
        "--window-budget=50000",
        "--window=3600",
        "--rate-limit-threshold=0.8",
    ])
    assert opts.rate_limit_max_wait_s == 600
    assert opts.window_budget == 50000
    assert opts.window_s == 3600
    assert opts.rate_limit_threshold == 0.8


def test_parse_window_budget_dollars_converts() -> None:
    opts = parse_build_args(["--window-budget=$15"])
    assert opts.window_budget == 1_000_000  # $15 at the notional rate


def test_parse_rate_limit_defaults() -> None:
    opts = parse_build_args(["epic-99"])
    assert opts.rate_limit_max_wait_s == 18000
    assert opts.window_budget == 0  # off by default → no proactive gating
    assert opts.window_s == 18000
    assert opts.rate_limit_threshold == 1.0


@pytest.mark.parametrize("bad", [
    "--rate-limit-max-wait=-1",
    "--window=0",
    "--rate-limit-threshold=0",
    "--rate-limit-threshold=1.5",
])
def test_parse_rate_limit_invalid_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_build_args([bad])


# ---------------------------------------------------------------------------
# AC: reactive 429 within the cap → auto-wait + resume the same run
# ---------------------------------------------------------------------------

def test_reactive_429_within_cap_auto_waits_and_resumes(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    sleeps = _Sleeps()
    # A short retry-after (well within the default ~5h cap) on the first story's
    # build stage: the controller waits, then the same run resumes and finishes.
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="retry-after", retry_after_s=120),
    )
    result = run_build(
        _opts(), queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True, sleep_fn=sleeps,
    )
    # The whole batch still completes — the throttle paused, it did not kill it.
    assert result.rate_limited is False
    assert result.completed == 3
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"
    # It actually waited the retry-after in-process.
    assert sum(sleeps.calls) == 120


def test_reactive_429_does_not_burn_a_bugfix_attempt(tmp_path: Path) -> None:
    # AC: a hard 429 mid-stage is a recoverable pause, never a stage FAILED — so
    # no FAILED build attempt and no bugfix row is ever recorded for the throttle.
    db = tmp_path / "ledger.db"
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="429", retry_after_s=60),
    )
    run_build(
        _opts(), queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True, sleep_fn=_Sleeps(),
    )
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test inspects the state machine
        rows = conn.execute(
            "SELECT stage_name, status FROM stages WHERE story_id = 's1-001'"
        ).fetchall()
    stages = [(r["stage_name"], r["status"]) for r in rows]
    # No bugfix was ever dispatched, and no build attempt was marked FAILED.
    assert not any(name == "bugfix" for name, _ in stages)
    assert not any(name == "build" and st == "FAILED" for name, st in stages)
    # The build did succeed on the retried (fresh) attempt.
    assert any(name == "build" and st == "DONE" for name, st in stages)


# ---------------------------------------------------------------------------
# AC: reset beyond the cap → durable RATE_LIMITED park (resumable, not terminal)
# ---------------------------------------------------------------------------

def test_reactive_429_beyond_cap_parks_rate_limited(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    sleeps = _Sleeps()
    # A reset a full window away with a tiny auto-wait cap → park, do not hold.
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,  # keep throttling — it must park, not spin
    )
    result = run_build(
        _opts(rate_limit_max_wait_s=300, window_s=18000),
        queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True,
        sleep_fn=sleeps, clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    assert result.rate_limit_waited_s == 0  # parked immediately, never auto-waited
    assert result.completed == 0
    sleeps_made = sum(sleeps.calls)
    assert sleeps_made == 0
    ledger = Ledger(db)
    # The run is RATE_LIMITED — resumable, NOT terminal, NOT NEEDS_ATTENTION.
    assert ledger.run_row(result.run_id)["status"] == "RATE_LIMITED"
    assert ledger.latest_resumable_run("epic-99") == result.run_id
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert statuses["s1-001"] == "RATE_LIMITED"


def test_rate_limited_park_logs_distinct_reason(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,
    )
    result = run_build(
        _opts(rate_limit_max_wait_s=300),
        queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001
        msgs = [
            r["message"] for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ?", (result.run_id,)
            ).fetchall()
        ]
    park = [m for m in msgs if "parking run RATE_LIMITED" in m]
    assert park, "expected a distinct RATE_LIMITED park event"
    # It is framed as waiting for time, explicitly not a failure / human attention.
    assert any("waiting for time" in m for m in park)


# ---------------------------------------------------------------------------
# AC: beyond-cap park → `sdlc resume` continues the same run cleanly
# (also covers interrupted-while-paused resume: the ledger row is RATE_LIMITED)
# ---------------------------------------------------------------------------

_SAMPLE_EPIC = """# Epic 88

##### Story 88.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 88.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _make_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-88-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def _build_parked(tmp_path: Path):
    """Build epic-88 so the first story's build stage parks RATE_LIMITED."""
    from sdlc.discovery import discover_queue

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-88", tmp_path)
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "88.1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,
    )
    opts = BuildOptions(
        scope="epic-88", skip_preflight=True, sequential=True,
        rate_limit_max_wait_s=300,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db), dispatcher=dispatcher,
        preflight=lambda: True, sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    return db, result


def test_resume_after_window_reopens_completes_run(tmp_path: Path) -> None:
    # The window has since reopened: resuming with a clean dispatcher (no throttle)
    # continues from where it parked and finishes the whole run.
    db, result = _build_parked(tmp_path)
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
    )
    assert resumed.run_id == result.run_id
    assert resumed.rate_limited is False
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert all(v == "DONE" for v in statuses.values()), statuses
    assert ledger.run_row(result.run_id)["status"] == "DONE"


def test_resume_while_still_limited_reparks(tmp_path: Path) -> None:
    # Resumed while the window is still closed (the throttle persists, reset still
    # beyond the cap) → re-parks RATE_LIMITED rather than failing.
    db, result = _build_parked(tmp_path)
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "88.1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,
    )
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert resumed.rate_limited is True
    ledger = Ledger(db)
    assert ledger.run_row(result.run_id)["status"] == "RATE_LIMITED"
    # Still resumable for a later, post-reset attempt.
    assert ledger.latest_resumable_run("epic-88") == result.run_id


def test_resume_repark_renders_view(tmp_path: Path) -> None:
    # A resume that re-parks RATE_LIMITED renders the live view exactly once for
    # its own run before returning (parity with run_build's park close-out).
    db, result = _build_parked(tmp_path)
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "88.1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,
    )
    rendered: list[str] = []
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
        render_view=rendered.append,
    )
    assert resumed.rate_limited is True
    assert rendered == [result.run_id]


# ---------------------------------------------------------------------------
# AC: configured rolling-window token budget (proactive gate)
# ---------------------------------------------------------------------------

def test_window_budget_within_cap_waits_and_continues(tmp_path: Path) -> None:
    # One story accrues 4 stages' tokens; a small per-window budget trips the
    # proactive gate before the 2nd story. With a short window (within the cap),
    # the controller waits, reopens the window, and finishes the run.
    db = tmp_path / "ledger.db"
    sleeps = _Sleeps()
    budget = 2 * _SAMPLE_STAGE_TOKENS  # tripped after the first story's 4 stages
    result = run_build(
        _opts(window_budget=budget, window_s=100, rate_limit_max_wait_s=18000),
        queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        sleep_fn=sleeps, clock=lambda: 0.0,
    )
    assert result.rate_limited is False
    assert result.completed == 3
    assert sum(sleeps.calls) > 0  # it paused at least once on the window budget


def test_window_budget_beyond_cap_parks(tmp_path: Path) -> None:
    # A configured window budget whose reset is beyond the auto-wait cap parks.
    db = tmp_path / "ledger.db"
    budget = 2 * _SAMPLE_STAGE_TOKENS
    result = run_build(
        _opts(window_budget=budget, window_s=999_999, rate_limit_max_wait_s=300),
        queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    assert result.completed == 1  # only the first story finished before the gate
    assert Ledger(db).run_row(result.run_id)["status"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# AC: graceful no-signal degradation — a non-rate-limit failure is unchanged
# ---------------------------------------------------------------------------

_THREE_STORY_EPIC = """# Epic 77

##### Story 77.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 77.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 77.1-003: Three
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def test_window_config_persisted_and_resume_makes_forward_progress(tmp_path: Path) -> None:
    # The window budget + cap are persisted in the run config and re-enforced on
    # resume — but a resume must treat the reopened window as fresh and make
    # forward progress, never re-park forever on pre-park spend (regression for
    # the durable-RATE_LIMITED-resume infinite-repark bug).
    from sdlc.discovery import discover_queue

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-77-sample.md").write_text(_THREE_STORY_EPIC, encoding="utf-8")
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-77", tmp_path)
    # Budget ≈ one story's accrual, window beyond the cap → parks after each story.
    opts = BuildOptions(
        scope="epic-77", skip_preflight=True, sequential=True,
        window_budget=_SAMPLE_STAGE_TOKENS, window_s=999_999,
        rate_limit_max_wait_s=300,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True, sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    assert result.completed == 1  # story 1 built, then parked before story 2

    # First resume *after the window has reopened* (clock past the parked reset):
    # the window starts fresh, so story 2 IS dispatched (forward progress), then
    # it re-parks before story 3.
    d2 = FakeDispatcher()
    r2 = run_resume(
        "epic-77", ledger=Ledger(db), dispatcher=d2, root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 1_000_000.0,
    )
    assert r2.rate_limited is True
    assert d2.calls, "resume must dispatch story 2 — not re-park with zero progress"
    statuses = {r["story_id"]: r["status"] for r in Ledger(db).story_rows(result.run_id)}
    assert sum(1 for v in statuses.values() if v == "DONE") == 2

    # Second resume (clock past the new reset): story 3 builds, run finishes.
    r3 = run_resume(
        "epic-77", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 2_000_000.0,
    )
    assert r3.rate_limited is False
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def _build_window_parked(tmp_path: Path):
    """Build the 2-story epic-88 so it parks on a beyond-cap window budget."""
    from sdlc.discovery import discover_queue

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-88", tmp_path)
    opts = BuildOptions(
        scope="epic-88", skip_preflight=True, sequential=True,
        window_budget=_SAMPLE_STAGE_TOKENS, window_s=999_999,
        rate_limit_max_wait_s=300,
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True, sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    # The reset epoch is persisted so a resume can honour it (≈ window_s away).
    assert Ledger(db).run_config(result.run_id)["rate_limit_reset_at"] == 999_999.0
    return db, result


def test_resume_before_reset_reparks_without_dispatching(tmp_path: Path) -> None:
    # Regression: resuming a window-budget-parked run *before* its reset epoch
    # must NOT bypass the parked reset and dispatch early — it re-parks (the
    # remaining wait is beyond the cap), dispatching nothing into the closed window.
    db, result = _build_window_parked(tmp_path)
    dispatcher = FakeDispatcher()
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 100.0,  # well before reset_at=999_999
    )
    assert resumed.rate_limited is True
    assert dispatcher.calls == [], "must not dispatch into a still-closed window"
    assert Ledger(db).run_row(result.run_id)["status"] == "RATE_LIMITED"


def test_resume_shortly_before_reset_waits_then_proceeds(tmp_path: Path) -> None:
    # When the remaining time to the reset is within the auto-wait cap, a resume
    # waits in-process for the window to reopen, then dispatches (no early bypass,
    # but no needless park either).
    db, result = _build_window_parked(tmp_path)
    sleeps = _Sleeps()
    # reset_at = 999_999; resume 200s before it → wait 200s (≤ cap 300), then run.
    resumed = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        sleep_fn=sleeps, clock=lambda: 999_799.0,
    )
    assert sum(sleeps.calls) == 200  # waited out the remaining window
    assert resumed.rate_limited is False
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def test_fallback_window_park_persists_reset_and_resume_honours_it(tmp_path: Path) -> None:
    # Regression: a throttle with NO explicit reset (no Retry-After, no reset
    # epoch) parks only because a full window exceeds the cap. The park must still
    # persist a fallback reset (now + window_s), or a resume would have nothing to
    # honour and dispatch early into the still-closed window.
    from sdlc.discovery import discover_queue

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    queue = discover_queue("epic-88", tmp_path)
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "88.1-001"),
        signal=RateLimitSignal(source="usage-limit"),  # no reset_at, no retry_after
        times=99,
    )
    opts = BuildOptions(
        scope="epic-88", skip_preflight=True, sequential=True,
        window_s=18000, rate_limit_max_wait_s=300,  # full window >> cap → parks
    )
    result = run_build(
        opts, queue=queue, ledger=Ledger(db), dispatcher=dispatcher,
        preflight=lambda: True, sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    # The fallback reset (now + window_s) was persisted despite no signal reset.
    assert Ledger(db).run_config(result.run_id)["rate_limit_reset_at"] == 18000.0

    # An early resume (before the fallback reset) honours it and re-parks rather
    # than dispatching into the still-closed window.
    early = FakeDispatcher()
    r_early = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=early, root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 50.0,
    )
    assert r_early.rate_limited is True
    assert early.calls == []

    # Once the fallback window has elapsed, the run resumes and finishes.
    r_done = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path,
        sleep_fn=_Sleeps(), clock=lambda: 30000.0,
    )
    assert r_done.rate_limited is False
    assert Ledger(db).run_row(result.run_id)["status"] == "DONE"


def test_cli_resume_reports_rate_limit(tmp_path: Path, monkeypatch) -> None:
    # The `sdlc resume` CLI renders the rate-limit summary and exits non-zero when
    # the resumed run re-parked RATE_LIMITED (rendering tested in isolation).
    from typer.testing import CliRunner

    import sdlc.cli as cli_mod
    from sdlc.resume import ResumeResult

    def fake_resume(*_a, **_k):
        return ResumeResult(
            run_id="run-1234abcd", resumed=0, completed=1, rate_limited=True,
            rate_limit_reset_at=999_999.0,
        )

    monkeypatch.setattr("sdlc.resume.run_resume", fake_resume)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    result = CliRunner().invoke(cli_mod.app, ["resume", "epic-88", "--db", str(db)])
    assert result.exit_code == 1
    assert "rate limit" in result.output.lower()


def test_non_rate_limit_failure_is_not_treated_as_pause(tmp_path: Path) -> None:
    # A generic build failure must still flow through the bugfix loop and end
    # FAILED — rate-limit handling must not swallow ordinary failures (AC7).
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher(overrides={
        ("build", "s1-001"): {"build_status": "FAILED", "error_summary": "boom"},
        ("bugfix", "s1-001"): {"fix_status": "GAVE_UP", "tests_passing": False},
    })
    result = run_build(
        _opts(), queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True, sleep_fn=_Sleeps(),
    )
    assert result.rate_limited is False
    assert result.story_status["s1-001"] == "FAILED"
