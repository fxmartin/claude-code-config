# ABOUTME: A fleet worker proves it can run an agent before its first registration (Story 35.2-007):
# ABOUTME: probe completes -> registers; stalls -> silent, retried every 60 s, registers on success.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.queue_worker import WorkerProfile
from sdlc.registry import Registry
from sdlc.scheduler import SchedulerConfig, run_queue
from sdlc.worker_selfcheck import SELF_CHECK_RETRY_SECONDS, SelfCheckResult

T0 = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
STALL = (
    "agent produced no output in 90s — a dialog is probably waiting on this Mac "
    "(Keychain / Privacy & Security) or ~/.claude resolves into a protected folder"
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProc:
    pid = 91001

    def poll(self):
        return 0

    def stop(self) -> None: ...


class Launcher:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), str(cwd)))
        return FakeProc()


class ScriptedCheck:
    """A self-check that fails ``failures`` times, then completes; remembers when it ran."""

    def __init__(self, clock: Clock, *, failures: int, reason: str = STALL) -> None:
        self.clock = clock
        self.failures = failures
        self.reason = reason
        self.ran_at: list[float] = []

    def __call__(self) -> SelfCheckResult:
        self.ran_at.append((self.clock() - T0).total_seconds())
        at = self.clock().isoformat()
        if len(self.ran_at) <= self.failures:
            return SelfCheckResult(ok=False, at=at, reason=self.reason)
        return SelfCheckResult(ok=True, at=at)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def _profile() -> WorkerProfile:
    return WorkerProfile(
        name="m3max", host="macbook-pro-m3-max", pools=["claude-m3"], harnesses=["claude"],
        repos=["alpha"],
    )


def _store(tmp_path) -> QueueStore:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


class StopAfter:
    """Interrupts the loop once ``seconds`` of fake time have passed (Ctrl-C / SIGTERM)."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        self.clock, self.limit = clock, seconds

    def __call__(self, seconds: float) -> None:
        self.clock.advance(seconds)
        if (self.clock() - T0).total_seconds() >= self.limit:
            raise KeyboardInterrupt


def _run(tmp_path, store, *, self_check, clock, follow=True, stop_after=None, echo=None):
    launcher = Launcher()
    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, follow=follow, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=StopAfter(clock, stop_after) if stop_after is not None else clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=echo or (lambda _line: None),
        identity="m3max",
        self_check=self_check,
    )
    return result, launcher


def test_a_completed_probe_registers_the_worker_with_its_self_check(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    check = ScriptedCheck(clock, failures=0)

    _run(tmp_path, store, self_check=check, clock=clock, follow=False)

    worker = store.get_worker("m3max")
    assert worker is not None
    assert worker.self_check == {"ok": True, "at": T0.isoformat(), "reason": None}
    assert check.ran_at == [0]  # once: a passed check is not re-run every beat


def test_a_stalled_probe_does_not_register_and_the_worker_stays_up(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    check = ScriptedCheck(clock, failures=10_000)

    # follow=False would drain and exit on an empty queue; a failing self-check must not.
    result, _ = _run(tmp_path, store, self_check=check, clock=clock, follow=False, stop_after=500)

    assert result.interrupted  # only the interrupt ended it — it never exited on its own
    assert store.get_worker("m3max") is None
    assert store.list_workers() == []


def test_a_stalled_probe_is_retried_every_sixty_seconds(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    check = ScriptedCheck(clock, failures=10_000)

    _run(tmp_path, store, self_check=check, clock=clock, stop_after=200)

    gaps = [b - a for a, b in zip(check.ran_at, check.ran_at[1:])]
    assert check.ran_at[0] == 0
    assert gaps and all(gap >= SELF_CHECK_RETRY_SECONDS for gap in gaps)
    assert len(check.ran_at) == 4  # t=0, 60, 120, 180


def test_the_worker_registers_the_moment_a_later_probe_completes(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    check = ScriptedCheck(clock, failures=2)
    seen: list[bool] = []
    original = store.register_worker

    def spy(name, **kwargs):
        seen.append(kwargs["self_check"]["ok"])
        return original(name, **kwargs)

    store.register_worker = spy  # type: ignore[method-assign]

    _run(tmp_path, store, self_check=check, clock=clock, stop_after=150)

    assert check.ran_at == [0, 60, 120]
    assert seen and all(seen)  # never a registration before the probe completed
    assert store.get_worker("m3max").self_check["ok"] is True
    assert store.get_worker("m3max").registered_at == (T0 + timedelta(seconds=120)).isoformat()


def test_a_stalled_worker_claims_no_job(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    repo = tmp_path / "alpha"
    repo.mkdir()
    job = store.add_job(repo=str(repo), kind="fix", scope="1")
    check = ScriptedCheck(clock, failures=10_000)

    _, launcher = _run(tmp_path, store, self_check=check, clock=clock, stop_after=200)

    assert launcher.calls == []
    assert store.get_job(job).state == "queued"


def test_a_job_queued_during_the_stall_runs_once_the_probe_completes(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    repo = tmp_path / "alpha"
    repo.mkdir()
    store.add_job(repo=str(repo), kind="fix", scope="1")
    check = ScriptedCheck(clock, failures=1)

    _, launcher = _run(tmp_path, store, self_check=check, clock=clock, stop_after=150)

    assert len(launcher.calls) == 1


def test_the_failure_is_logged_once_with_the_cause_not_every_retry(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    lines: list[str] = []

    _run(
        tmp_path, store, self_check=ScriptedCheck(clock, failures=10_000), clock=clock,
        stop_after=400, echo=lines.append,
    )

    assert lines.count(f"worker self-check failed: {STALL}") == 1


def test_a_changed_cause_is_logged_again(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    lines: list[str] = []
    reasons = iter([STALL, STALL, "agent probe failed (exit 1): Invalid API key"])

    def check() -> SelfCheckResult:
        return SelfCheckResult(ok=False, at=clock().isoformat(), reason=next(reasons, "x"))

    _run(tmp_path, store, self_check=check, clock=clock, stop_after=130, echo=lines.append)

    failed = [line for line in lines if line.startswith("worker self-check failed")]
    assert failed == [
        f"worker self-check failed: {STALL}",
        "worker self-check failed: agent probe failed (exit 1): Invalid API key",
    ]


def test_recovery_is_announced(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    lines: list[str] = []

    _run(
        tmp_path, store, self_check=ScriptedCheck(clock, failures=1), clock=clock,
        stop_after=90, echo=lines.append,
    )

    assert any(line.startswith("worker self-check passed") for line in lines)


def test_a_drain_without_a_self_check_registers_as_before(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()

    _run(tmp_path, store, self_check=None, clock=clock, follow=False)

    worker = store.get_worker("m3max")
    assert worker is not None and worker.self_check is None
