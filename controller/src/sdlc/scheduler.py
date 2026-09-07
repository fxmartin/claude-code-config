# ABOUTME: The `sdlc queue run` scheduler — leased claims, per-repo exclusivity, slot cap.
# ABOUTME: Story 32.1-002. A thin loop over queue.py + registry.py that spawns build/fix/resume.

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from sdlc.queue import JobRecord, QueueStore
from sdlc.registry import Registry, RunRecord, pid_alive

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RENEW_SECONDS",
    "DEFAULT_SLOTS",
    "JobProcess",
    "SchedulerConfig",
    "SchedulerResult",
    "agent_slots",
    "controller_argv",
    "job_argv",
    "run_queue",
]

# Lease numbers. 90s renewed every 30s is Hyqs's tested figure (see the epic's
# technical notes): long enough that a busy scheduler never loses a job to a
# transient stall, short enough that a killed scheduler's work is reclaimable
# within a minute and a half rather than a night.
DEFAULT_LEASE_SECONDS = 90
DEFAULT_RENEW_SECONDS = 30
# Host-wide agent-slot cap. Two is the conservative default for a laptop that
# is also FX's daily driver; raise it with `--slots`.
DEFAULT_SLOTS = 2
DEFAULT_POLL_SECONDS = 2.0

# Registry run statuses that mean *parked*, not finished: the run stopped short
# and is resumable by a human decision (raise the budget, approve the merge,
# wait out the rate limit). A job whose run ends in one of these mirrors it as
# ``blocked`` rather than ``failed`` — see :func:`terminal_job_state`.
_PARKED_RUN_STATUSES = frozenset(
    {"RATE_LIMITED", "AWAITING_APPROVAL", "NEEDS_ATTENTION"}
)

# How long a stopped child is given to die on SIGTERM before SIGKILL. Generous
# on purpose: a job subprocess unwinds a ledger and a git worktree on the way
# out, and the CI job container has no init reaper to tidy up after a rushed
# kill (CLAUDE.md, "Process semantics").
_STOP_GRACE_SECONDS = 10.0


class JobProcess(Protocol):
    """The scheduler's view of a launched job — a seam, so tests need no fork.

    Deliberately narrower than ``subprocess.Popen``: the loop only ever asks
    whether the job is still running and, on Ctrl-C, tells it to stop.
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def stop(self) -> None: ...


Launcher = Callable[[Sequence[str], Path], JobProcess]
Clock = Callable[[], datetime]
VersionCheck = Callable[[Path], object]


@dataclass
class SchedulerConfig:
    """Knobs for one `sdlc queue run` invocation."""

    slots: int = DEFAULT_SLOTS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    renew_seconds: int = DEFAULT_RENEW_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    # ``follow`` keeps the loop alive on an empty queue so an intake (a future
    # `sdlc listen`, Epic-30) can enqueue into a draining scheduler. The default
    # drains what is there and exits, which is what a night's batch wants.
    follow: bool = False


@dataclass
class SchedulerResult:
    """What one drain did — the summary `sdlc queue run` prints."""

    started: int = 0
    resumed: int = 0
    done: int = 0
    failed: int = 0
    parked: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "started": self.started,
            "resumed": self.resumed,
            "done": self.done,
            "failed": self.failed,
            "parked": self.parked,
            "interrupted": self.interrupted,
        }


@dataclass
class _InFlight:
    """One job the scheduler is currently driving."""

    job: JobRecord
    proc: JobProcess
    slots: int
    run_id: str | None
    last_renewed: datetime


class _PopenProcess:
    """A real job subprocess, isolated in its own process group.

    ``start_new_session`` is what makes :meth:`stop` a *group* kill: a job runs
    agents of its own, and signalling only the parent would strand them. On the
    way out we escalate SIGTERM → SIGKILL rather than assuming a prompt death.
    """

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()

    def stop(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self._proc.poll() is not None:
                return
            try:
                os.killpg(os.getpgid(self._proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                # Already gone, or not ours to signal — either way there is
                # nothing left to stop.
                return
            try:
                self._proc.wait(timeout=_STOP_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue


def controller_argv() -> list[str]:
    """The argv prefix that invokes this controller in a child process.

    Prefers the PATH-installed `sdlc` (the normal host install); falls back to
    running this very interpreter's ``sdlc.cli`` module, so a scheduler started
    from a source checkout (`uv run sdlc queue run`) dispatches the same code it
    is itself running rather than a stale global install.
    """
    binary = shutil.which("sdlc")
    if binary:
        return [binary]
    return [sys.executable, "-m", "sdlc.cli"]


def _frozen_options(job: JobRecord) -> list[str]:
    """The job's frozen CLI flag vector, or ``[]`` when there is none/it is junk.

    ``options`` is whatever `--enqueue` recorded; a corrupt value must degrade
    to "no flags" rather than crash a night's drain.
    """
    if not job.options:
        return []
    try:
        parsed = json.loads(job.options)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def agent_slots(job: JobRecord) -> int:
    """How many host agent slots ``job`` occupies while it runs.

    Slot accounting counts *agent subprocesses*, not runs: a job told to drive
    four workers costs four slots, so a cap of two never lets it start beside
    another job. A job that declares nothing costs one — the queue can only
    account for what the job's own frozen flags promise, and `sdlc build`'s
    in-run default is a ceiling on how many stories may overlap, not a promise
    that many agents are ever live at once. (A later story can tighten this by
    having a run report its live agent count; until then the declared cap is
    the honest figure, and it is the one `sdlc queue run` prints.)
    """
    options = _frozen_options(job)
    if "--sequential" in options:
        return 1
    for arg in options:
        if arg.startswith("--concurrency="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                return 1
    return 1


def job_argv(job: JobRecord, *, resume: bool, prefix: Sequence[str] | None = None) -> list[str]:
    """The command that runs ``job``.

    A *fresh* job replays its frozen flag vector verbatim — that vector already
    carries the scope as its first positional, so a job enqueued as
    ``sdlc build epic-3 --auto`` re-runs byte-identically. Nothing is added:
    no ``--allow-dirty``, no ``--force``, no stash. A job the repo's dirty-tree
    guard (#590) would refuse must be refused here too.

    A *reclaimed* job re-enters through ``sdlc resume --run <id>``, never a
    fresh ``build``/``fix`` — ``resume.py`` is the re-entry path and it picks
    each story up at the stage it died in.
    """
    argv = list(prefix) if prefix is not None else controller_argv()
    if resume:
        if not job.run_id:
            raise ValueError(f"job {job.id} has no run to resume")
        return argv + ["resume", "--run", job.run_id]
    options = _frozen_options(job)
    return argv + [job.kind] + (options or [job.scope])


def terminal_job_state(exit_code: int, run_status: str | None) -> str:
    """The job state mirroring how the run actually ended (AC6).

    The run's own registry status wins when it names a parked terminal — a
    RATE_LIMITED or AWAITING_APPROVAL run exits non-zero but is *resumable*,
    and calling that ``failed`` would bury it. Otherwise the exit code decides.
    """
    if run_status in _PARKED_RUN_STATUSES:
        return "blocked"
    return "done" if exit_code == 0 else "failed"


def _default_launcher(argv: Sequence[str], cwd: Path) -> JobProcess:
    """Spawn a job as a detached process group under ``cwd``.

    Runs as a subprocess, never in-process, so a job's crash cannot take the
    scheduler down with it and a process-group kill reaches the job's own
    agents.
    """
    proc = subprocess.Popen(list(argv), cwd=str(cwd), start_new_session=True)
    return _PopenProcess(proc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Scheduler:
    """The drain loop. See :func:`run_queue` for the public entry point."""

    def __init__(
        self,
        store: QueueStore,
        *,
        config: SchedulerConfig,
        registry: Registry,
        launcher: Launcher,
        clock: Clock,
        sleeper: Callable[[float], None],
        notifier: Callable[..., None],
        version_check: VersionCheck,
        echo: Callable[[str], None],
        identity: str,
    ) -> None:
        self._store = store
        self._config = config
        self._registry = registry
        self._launcher = launcher
        self._clock = clock
        self._sleep = sleeper
        self._notify = notifier
        self._version_check = version_check
        self._echo = echo
        self._identity = identity
        self._in_flight: dict[int, _InFlight] = {}
        self._result = SchedulerResult()

    # --- the loop ---------------------------------------------------------

    def run(self) -> SchedulerResult:
        try:
            while True:
                self._attach_runs()
                self._reap()
                self._renew()
                progressed = self._fill_slots()
                self._stamp_repo_busy()
                if not self._in_flight and not progressed and not self._config.follow:
                    break
                self._sleep(self._config.poll_seconds)
        except KeyboardInterrupt:
            self._result.interrupted = True
            self._shutdown()
        return self._result

    # --- admission --------------------------------------------------------

    def _used_slots(self) -> int:
        return sum(entry.slots for entry in self._in_flight.values())

    def _fill_slots(self) -> bool:
        """Claim and launch while slots and claimable work remain."""
        progressed = False
        # Bounded so a pathological race (every claim lost to another
        # scheduler) can never spin this pass forever.
        for _ in range(64):
            candidate = self._next_candidate()
            if candidate is None:
                break
            job, resume = candidate
            cost = agent_slots(job)
            used = self._used_slots()
            if used and used + cost > self._config.slots:
                break
            now = self._clock()
            if resume:
                claimed = self._store.reclaim_job(
                    job.id, claimed_by=self._identity,
                    lease_seconds=self._config.lease_seconds, now=now,
                )
            else:
                claimed = self._store.claim_job(
                    job.id, claimed_by=self._identity,
                    lease_seconds=self._config.lease_seconds, now=now,
                )
            if claimed is None:
                continue  # lost the race to another scheduler — try the next one
            self._start(claimed, resume=resume, cost=cost)
            progressed = True
        return progressed

    def _next_candidate(self) -> tuple[JobRecord, bool] | None:
        """The next job to drive, and whether it is a resume.

        Reclaimable work comes first: a run that already exists is half-paid
        for, and leaving it parked behind fresh work is how a night stalls.
        """
        for job in self._store.expired_running_jobs(now=self._clock()):
            if job.id in self._in_flight:
                continue
            if job.run_id is None:
                # Claimed but never started a run — nothing to resume, so put it
                # back in the queue and let the normal claim path take it. Take
                # ownership first: the previous holder may have already dropped
                # its `claimed_by`, and only the owner may release a claim.
                if self._store.reclaim_job(
                    job.id, claimed_by=self._identity,
                    lease_seconds=self._config.lease_seconds, now=self._clock(),
                ) is not None:
                    self._store.release_claim(
                        job.id, claimed_by=self._identity,
                        reason="reclaimed: no run had started", now=self._clock(),
                    )
                continue
            if self._run_is_live(job.run_id):
                # The run's own pid still answers — the lease lapsed but the
                # work did not. Two drivers on one run is exactly what the
                # registry guard exists to prevent.
                continue
            return job, True

        busy = self._store.running_repos()
        for job in self._store.peek_claimable(busy_repos=busy, now=self._clock()):
            return job, False
        return None

    def _stamp_repo_busy(self) -> None:
        """Explain a job that could have run but for its repo already being busy."""
        busy = self._store.running_repos()
        if not busy:
            return
        for job in self._store.peek_claimable(now=self._clock()):
            if job.repo in busy and job.reason != "repo busy":
                self._store.set_reason(job.id, "repo busy")

    # --- launch + reap ----------------------------------------------------

    def _start(self, job: JobRecord, *, resume: bool, cost: int) -> None:
        # Story 15.1-004, per job: a repo whose installed controller disagrees
        # with its own checkout would run this job on stale code. Park it with
        # the remedy instead — the check is cheap, offline, and repo-local, so
        # it runs for every job rather than once for the scheduler.
        finding = self._version_check(Path(job.repo))
        status = getattr(finding, "status", "CLEAN")
        if status != "CLEAN":
            detail = getattr(finding, "detail", "")
            remedy = getattr(finding, "remedy", "")
            reason = " — ".join(part for part in (detail, remedy) if part)
            self._store.finish_job(job.id, "blocked", reason=reason)
            self._result.parked += 1
            self._echo(f"job {job.id} parked (blocked): {reason}")
            self._announce(job, None, "blocked")
            return

        if cost > self._config.slots:
            # Never starve a job the host cap cannot fit: run it alone, and say
            # so — a silent over-subscription would be worse than a loud one.
            self._echo(
                f"job {job.id} wants {cost} agent slots (cap "
                f"{self._config.slots}) — running it alone"
            )

        argv = job_argv(job, resume=resume)
        try:
            proc = self._launcher(argv, Path(job.repo))
        except OSError as exc:
            self._store.finish_job(job.id, "failed", reason=f"could not launch: {exc}")
            self._result.failed += 1
            self._echo(f"job {job.id} could not be launched: {exc}")
            self._announce(job, None, "failed")
            return

        self._in_flight[job.id] = _InFlight(
            job=job, proc=proc, slots=cost, run_id=job.run_id,
            last_renewed=self._clock(),
        )
        if resume:
            self._result.resumed += 1
        else:
            self._result.started += 1
        verb = "resuming" if resume else "started"
        self._echo(
            f"{verb} job {job.id} ({job.kind} {job.scope}) in {job.repo} "
            f"[pid {proc.pid}, {cost} slot{'s' if cost != 1 else ''}]"
        )

    def _reap(self) -> None:
        for job_id, entry in list(self._in_flight.items()):
            code = entry.proc.poll()
            if code is None:
                continue
            del self._in_flight[job_id]
            run_status = self._run_status(entry.run_id)
            state = terminal_job_state(code, run_status)
            reason = None if state == "done" else f"run status {run_status or code}"
            self._store.finish_job(job_id, state, reason=reason)
            if state == "done":
                self._result.done += 1
            elif state == "blocked":
                self._result.parked += 1
            else:
                self._result.failed += 1
            self._echo(f"job {job_id} finished: {state}")
            self._announce(entry.job, entry.run_id, state)

    def _renew(self) -> None:
        now = self._clock()
        for job_id, entry in list(self._in_flight.items()):
            if (now - entry.last_renewed).total_seconds() < self._config.renew_seconds:
                continue
            held = self._store.renew_lease(
                job_id, claimed_by=self._identity,
                lease_seconds=self._config.lease_seconds, now=now,
            )
            entry.last_renewed = now
            if not held:
                # Another scheduler already reclaimed this job — it now owns the
                # run. Drop our bookkeeping; the child is that scheduler's to
                # reap, and killing it here would abort work we no longer own.
                del self._in_flight[job_id]
                self._echo(f"job {job_id}: lease lost, another scheduler took it over")

    # --- registry ---------------------------------------------------------

    def _attach_runs(self) -> None:
        """Link each in-flight job to the run its subprocess opened.

        `run_build`/`run_fix` register with ``pid=os.getpid()``, which is
        exactly the child we spawned — so the child's pid is the join key. The
        link is what lets a killed scheduler resume rather than restart.
        """
        pending = {
            entry.proc.pid: job_id
            for job_id, entry in self._in_flight.items()
            if entry.run_id is None
        }
        if not pending:
            return
        for record in self._registry.records():
            job_id = pending.get(record.pid)
            if job_id is None:
                continue
            self._in_flight[job_id].run_id = record.run_id
            self._store.attach_run(job_id, record.run_id)

    def _registry_record(self, run_id: str) -> RunRecord | None:
        for record in self._registry.records():
            if record.run_id == run_id:
                return record
        return None

    def _run_is_live(self, run_id: str) -> bool:
        record = self._registry_record(run_id)
        if record is None or record.finished_at:
            return False
        return pid_alive(record.pid)

    def _run_status(self, run_id: str | None) -> str | None:
        if run_id is None:
            return None
        record = self._registry_record(run_id)
        return record.status if record is not None else None

    # --- notify + shutdown ------------------------------------------------

    def _announce(self, job: JobRecord, run_id: str | None, state: str) -> None:
        """Announce a terminal job down the existing Telegram notify path (AC6)."""
        self._notify(
            "run_finished",
            repo=Path(job.repo).name,
            subject=f"queue job {job.id} ({job.kind} {job.scope})",
            terminal=state.upper(),
            run=run_id or "",
        )

    def _shutdown(self) -> None:
        """Ctrl-C: stop every child, then hand its lease back (AC5).

        Stopping first and releasing second is deliberate — a lease released
        while its child is still alive would invite a second scheduler to drive
        the same run.
        """
        self._echo("interrupted — stopping jobs and releasing leases")
        for job_id, entry in list(self._in_flight.items()):
            try:
                entry.proc.stop()
            except OSError as exc:
                self._echo(f"job {job_id}: could not stop pid {entry.proc.pid}: {exc}")
            self._store.release_claim(
                job_id, claimed_by=self._identity,
                reason="scheduler interrupted", now=self._clock(),
            )
            self._echo(f"job {job_id}: lease released")
        self._in_flight.clear()


def run_queue(
    store: QueueStore,
    *,
    config: SchedulerConfig | None = None,
    registry: Registry | None = None,
    launcher: Launcher | None = None,
    clock: Clock | None = None,
    sleeper: Callable[[float], None] | None = None,
    notifier: Callable[..., None] | None = None,
    version_check: VersionCheck | None = None,
    echo: Callable[[str], None] | None = None,
    identity: str | None = None,
) -> SchedulerResult:
    """Drain the host queue: claim jobs under a lease and run them as subprocesses.

    The foreground loop behind `sdlc queue run` (Story 32.1-002). Each pass:

    1. link in-flight jobs to the runs their subprocesses registered;
    2. reap finished jobs and mirror the run's terminal status onto the job;
    3. renew our leases;
    4. reclaim any lapsed job whose run is genuinely dead and resume it, then
       claim fresh work while agent slots and non-busy repos remain.

    Every collaborator is injectable so the loop is testable without forking:
    ``launcher`` spawns a job, ``clock``/``sleeper`` drive time, ``registry``
    supplies run liveness, ``version_check`` is Story 15.1-004's per-repo check,
    and ``notifier`` is the Telegram path.

    Daemonisation is deliberately *not* built here: the documented path is the
    Epic-30 30.3-001 LaunchAgent pattern (KeepAlive, standard logs, secrets from
    the env/file convention) wrapping this same foreground command.
    """
    from sdlc.doctor import check_controller_version
    from sdlc.notify import notify

    scheduler = _Scheduler(
        store,
        config=config or SchedulerConfig(),
        registry=registry if registry is not None else Registry(),
        launcher=launcher or _default_launcher,
        clock=clock or _utc_now,
        sleeper=sleeper or time.sleep,
        notifier=notifier or notify,
        version_check=version_check or check_controller_version,
        echo=echo or print,
        identity=identity or f"{socket.gethostname()}:{os.getpid()}",
    )
    return scheduler.run()
