# ABOUTME: The `sdlc queue run` scheduler — leased claims, per-repo exclusivity, slot cap.
# ABOUTME: Stories 32.1-002 + 32.2-002. A thin loop over queue.py + registry.py that
# ABOUTME: spawns build/fix/resume/reconcile, including the approval park and auto-resume.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from sdlc.approval import ApprovalVerdict, poll_approval
from sdlc.queue import JobRecord, QueueStore
from sdlc.registry import Registry, RunRecord, pid_alive
from sdlc.risk_gate import RISK_APPROVED_LABEL

__all__ = [
    "DEFAULT_APPROVAL_POLL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RENEW_SECONDS",
    "DEFAULT_SLOTS",
    "MAX_APPROVAL_POLL_SECONDS",
    "MIN_APPROVAL_POLL_SECONDS",
    "JobProcess",
    "SchedulerConfig",
    "SchedulerResult",
    "agent_slots",
    "approval_poll_interval",
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

# How often a *parked* job's change request is re-read (Story 32.2-002). Five
# minutes is the story's default: fast enough that FX's morning label is acted
# on before the coffee is poured, slow enough that a night of parked jobs costs
# a couple of hundred API reads rather than tens of thousands. The bounds are
# hard: below the floor the poll is an abuse of the host's rate limit, above the
# ceiling an approval could sit unnoticed for over an hour, which defeats the
# story.
DEFAULT_APPROVAL_POLL_SECONDS = 300.0
MIN_APPROVAL_POLL_SECONDS = 30.0
MAX_APPROVAL_POLL_SECONDS = 3600.0

# Registry *terminal* statuses that mean parked-for-a-human rather than failed:
# the run reached an end state, but one a human decision reopens (approve the
# merge, look at the stuck story). A job whose run ends in one of these mirrors
# it as ``blocked`` rather than ``failed`` — see :func:`terminal_job_state`.
#
# ``RATE_LIMITED`` is listed defensively, not because the registry carries it
# today: it is the *ledger's* vocabulary (``build.py`` writes it via
# ``run_update_status``), and a rate-limit park deliberately skips
# ``finalize_run``, so the registry record is left *open* instead. That case is
# caught by the open-record check in :func:`terminal_job_state`, which is the
# one that matters; keeping the name here costs nothing and means a future
# writer (Story 32.2-001's host-level pause) cannot regress the mapping.
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
# One read of a parked job's change request: ``(repo_root, cr_number)`` →
# verdict, or None when the host could not be read (Story 32.2-002).
ApprovalProbe = Callable[[Path, int], "ApprovalVerdict | None"]


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
    # How often a parked job's change request is re-read (Story 32.2-002).
    # Clamped by :func:`approval_poll_interval` before use.
    approval_poll_seconds: float = DEFAULT_APPROVAL_POLL_SECONDS


@dataclass
class SchedulerResult:
    """What one drain did — the summary `sdlc queue run` prints."""

    started: int = 0
    resumed: int = 0
    # Jobs whose parked change request turned out to have been merged by hand,
    # so the queue reconciled the run instead of resuming it (Story 32.2-002).
    reconciled: int = 0
    done: int = 0
    failed: int = 0
    parked: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "started": self.started,
            "resumed": self.resumed,
            "reconciled": self.reconciled,
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


def _carries_a_scope(options: Sequence[str]) -> bool:
    """Whether ``options`` already contains the scope as a bare positional.

    Both parsers (``parse_build_args``, ``parse_fix_args``) treat every token
    that does not start with ``--`` as a positional scope token, with exactly
    one exception: ``sdlc build --harness <spec>``, whose value is a second
    token. Mirroring that rule here is what keeps a ``--harness`` value from
    being mistaken for a scope the vector already carries.
    """
    tokens = iter(options)
    for token in tokens:
        if token == "--harness":
            next(tokens, None)  # the space-separated form's value, not a scope
            continue
        if not token.startswith("--"):
            return True
    return False


def job_argv(job: JobRecord, *, resume: bool, prefix: Sequence[str] | None = None) -> list[str]:
    """The command that runs ``job``.

    A *fresh* job replays its frozen flag vector, prefixed with the job's own
    ``scope`` unless that vector already carries one. Both shapes reach this
    store: ``--enqueue`` freezes the whole argv (scope included), so a job
    enqueued as ``sdlc build epic-3 --auto`` re-runs byte-identically; but
    ``sdlc queue add build epic-3 --options '["--auto"]'`` records the scope in
    its own column and *only flags* in ``options``, exactly as that flag's help
    documents. Taking the vector as-is there dropped the positional and silently
    widened the job to the ``all`` default — every epic in the repo. Nothing
    else is added: no ``--allow-dirty``, no ``--force``, no stash. A job the
    repo's dirty-tree guard (#590) would refuse must be refused here too.

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
    if _carries_a_scope(options):
        return argv + [job.kind] + options
    return argv + [job.kind, job.scope] + options


def approval_poll_interval(seconds: float) -> float:
    """Clamp a requested approval poll interval into its documented bounds (AC4).

    A single place so the CLI flag, the config default and the scheduler agree.
    The floor protects the host's API rate limit from a `--follow` loop that
    would otherwise re-read every parked CR on every two-second pass; the
    ceiling keeps the story's promise that a morning's label is acted on
    without a human ever typing `sdlc resume`.
    """
    return max(MIN_APPROVAL_POLL_SECONDS, min(MAX_APPROVAL_POLL_SECONDS, float(seconds)))


def reconcile_argv(job: JobRecord, *, prefix: Sequence[str] | None = None) -> list[str]:
    """The command that reconciles ``job``'s run after a hand-merged CR (AC3).

    `sdlc reconcile <run>` already exists and is the *only* thing that should
    run here: it fetches origin, proves the story's branch landed on the base,
    flips it DONE with a ``source="reconcile"`` audit event, and re-terminals
    the run. Resuming instead would re-enter a merge stage against a CR that is
    already merged, and hand-merging plus a manual `reconcile` is exactly the
    chore this story removes.
    """
    if not job.run_id:
        raise ValueError(f"job {job.id} has no run to reconcile")
    argv = list(prefix) if prefix is not None else controller_argv()
    return argv + ["reconcile", job.run_id]


def terminal_job_state(
    exit_code: int, run_status: str | None, *, run_finished: bool = True
) -> str:
    """The job state mirroring how the run actually ended (AC6).

    The exit code alone lies about the paths that matter most. A run parked
    *resumably* — a rate-limit window, the interactive cost gate, a
    ``--budget-policy=pause`` stop — exits non-zero but deliberately never
    stamps a terminal status: ``_rate_limit_close_out``/``_cost_gate_close_out``
    skip ``finalize_run`` precisely so ``latest_resumable_run`` can still find
    the run, which leaves its registry record **open** (no ``finished_at``,
    status still ``IN_PROGRESS``). Reading only the exit code called all three
    ``failed`` and buried work `sdlc resume` could have finished.

    So the questions, in order:

    1. Did a run exist that never reached a terminal, while its child exited
       non-zero? Then it is *parked*, not failed — ``blocked``. Exit 0 is the
       stronger signal and wins, so a successful run is never parked.
    2. Did the run reach a terminal that still needs a human (``AWAITING_APPROVAL``
       / ``NEEDS_ATTENTION``)? Also ``blocked``.
    3. Otherwise the exit code decides.

    A job with no run at all (``run_status is None`` — ``--dry-run``, a launch
    that aborted before registering) falls straight through to the exit code.
    """
    if run_status is not None and not run_finished and exit_code != 0:
        return "blocked"
    if run_status in _PARKED_RUN_STATUSES:
        return "blocked"
    return "done" if exit_code == 0 else "failed"


def _awaiting_approval_pr(record: RunRecord | None) -> int | None:
    """The change request a run parked ``AWAITING_APPROVAL`` is waiting on.

    The registry names the run's ledger (``RunRecord.db``) and the ledger is the
    only place the story↔PR link lives, so the queue reads it there — read-only,
    through the same ``story_rows`` view every other surface uses.

    A run may park several stories at once. The queue watches the first of them
    (story rows come back in a stable order), which converges rather than
    stalls: approving it resumes the run, the run re-parks on whichever story is
    still unapproved, and the job re-parks on *that* CR. Returns None — leaving
    the pre-32.2 ``blocked`` terminal in place — whenever no PR can be resolved,
    because a park with nothing to poll is a dead end that should say so.
    """
    if record is None or not record.db:
        return None
    try:
        from sdlc.ledger_view import Ledger

        rows = Ledger(Path(record.db)).story_rows(record.run_id)
    except Exception:  # noqa: BLE001 — a ledger hiccup must not crash the drain
        return None
    for row in rows:
        if row.get("status") != "AWAITING_APPROVAL":
            continue
        pr_number = row.get("pr_number")
        if isinstance(pr_number, int):
            return pr_number
    return None


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


def _default_approval_probe(root: Path, pr_number: int) -> ApprovalVerdict | None:
    """The real, read-only host poll behind a parked job (Story 32.2-002)."""
    return poll_approval(root, pr_number)


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
        approval_probe: ApprovalProbe,
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
        self._approval_probe = approval_probe
        self._echo = echo
        self._identity = identity
        self._in_flight: dict[int, _InFlight] = {}
        self._result = SchedulerResult()
        self._poll_interval = approval_poll_interval(config.approval_poll_seconds)

    # --- the loop ---------------------------------------------------------

    def run(self) -> SchedulerResult:
        try:
            while True:
                self._attach_runs()
                self._reap()
                self._renew()
                polled = self._poll_parked()
                progressed = self._fill_slots() or polled
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
            self._start(claimed, action="resume" if resume else "start", cost=cost)
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
        fix_busy = self._store.running_repos(kind="fix")
        for job in self._store.peek_claimable(
            busy_repos=busy, fix_busy_repos=fix_busy, now=self._clock()
        ):
            return job, False
        return None

    def _stamp_repo_busy(self) -> None:
        """Explain a job that could have run but for its repo already being busy.

        Story 32.1-003: a ``build`` candidate is only genuinely blocked by a
        running ``fix`` (build/build now overlaps freely), so it must not be
        stamped "repo busy" just because another build is live in its repo.
        """
        busy = self._store.running_repos()
        fix_busy = self._store.running_repos(kind="fix")
        if not busy:
            return
        for job in self._store.peek_claimable(now=self._clock()):
            blocked = job.repo in fix_busy or (job.kind == "fix" and job.repo in busy)
            if blocked and job.reason != "repo busy":
                self._store.set_reason(job.id, "repo busy")

    # --- launch + reap ----------------------------------------------------

    def _start(self, job: JobRecord, *, action: str, cost: int) -> bool:
        """Launch ``job``; True when a child is actually in flight.

        ``action`` is what the queue decided this job needs now: ``start`` a
        fresh `build`/`fix`, ``resume`` an existing run, or ``reconcile`` one
        whose change request was merged by hand (Story 32.2-002). Everything
        else — the version guard, slot accounting, launch-failure handling,
        bookkeeping — is identical across the three, which is exactly why they
        share this one path.
        """
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
            return False

        if cost > self._config.slots:
            # Never starve a job the host cap cannot fit: run it alone, and say
            # so — a silent over-subscription would be worse than a loud one.
            self._echo(
                f"job {job.id} wants {cost} agent slots (cap "
                f"{self._config.slots}) — running it alone"
            )

        argv = (
            reconcile_argv(job) if action == "reconcile"
            else job_argv(job, resume=action == "resume")
        )
        try:
            proc = self._launcher(argv, Path(job.repo))
        except OSError as exc:
            self._store.finish_job(job.id, "failed", reason=f"could not launch: {exc}")
            self._result.failed += 1
            self._echo(f"job {job.id} could not be launched: {exc}")
            self._announce(job, None, "failed")
            return False

        self._in_flight[job.id] = _InFlight(
            job=job, proc=proc, slots=cost, run_id=job.run_id,
            last_renewed=self._clock(),
        )
        if action == "resume":
            self._result.resumed += 1
        elif action == "reconcile":
            self._result.reconciled += 1
        else:
            self._result.started += 1
        verb = {"resume": "resuming", "reconcile": "reconciling"}.get(action, "started")
        self._echo(
            f"{verb} job {job.id} ({job.kind} {job.scope}) in {job.repo} "
            f"[pid {proc.pid}, {cost} slot{'s' if cost != 1 else ''}]"
        )
        return True

    def _reap(self) -> None:
        for job_id, entry in list(self._in_flight.items()):
            code = entry.proc.poll()
            if code is None:
                continue
            del self._in_flight[job_id]
            record = (
                self._registry_record(entry.run_id) if entry.run_id else None
            )
            run_status = record.status if record is not None else None
            run_finished = bool(record.finished_at) if record is not None else True
            state = terminal_job_state(code, run_status, run_finished=run_finished)
            # Story 32.2-002: `AWAITING_APPROVAL` is terminal for the run — it
            # must stay so, the bugfix loop cannot self-approve — but not for the
            # job. When the change request behind it is knowable, the queue takes
            # over the wait instead of stamping a `blocked` dead end.
            if state == "blocked" and run_status == "AWAITING_APPROVAL":
                pr_number = _awaiting_approval_pr(record)
                if pr_number is not None:
                    self._park_for_approval(entry, pr_number)
                    continue
            reason = self._finish_reason(state, code, run_status, entry.run_id,
                                         run_finished)
            self._store.finish_job(job_id, state, reason=reason)
            if state == "done":
                self._result.done += 1
            elif state == "blocked":
                self._result.parked += 1
            else:
                self._result.failed += 1
            self._echo(f"job {job_id} finished: {state}")
            self._announce(entry.job, entry.run_id, state)

    @staticmethod
    def _finish_reason(
        state: str, exit_code: int, run_status: str | None, run_id: str | None,
        run_finished: bool,
    ) -> str | None:
        """Why a job ended, in words an operator can act on.

        A run parked open has no terminal status to quote — saying "run status
        IN_PROGRESS" would read as a bug rather than as the resumable park it
        is — so it names the re-entry command instead.
        """
        if state == "done":
            return None
        if state == "blocked" and not run_finished and run_id:
            return (
                f"run paused and still resumable — clear the cause "
                f"(rate-limit window, --cost-threshold, --budget) then "
                f"`sdlc resume --run {run_id}`"
            )
        return f"run status {run_status or exit_code}"

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

    # --- the approval park (Story 32.2-002) -------------------------------

    def _next_poll(self) -> datetime:
        """When this job's change request may next be read."""
        return self._clock() + timedelta(seconds=self._poll_interval)

    def _park_for_approval(self, entry: _InFlight, pr_number: int) -> None:
        """Hand a job's approval wait to the queue instead of ending it (AC1)."""
        job = entry.job
        reason = (
            f"awaiting approval on #{pr_number} — polling every "
            f"{int(self._poll_interval)}s for the `{RISK_APPROVED_LABEL}` label "
            f"or an approving review"
        )
        self._store.park_job(
            job.id, pr_number=pr_number, reason=reason, poll_after=self._next_poll()
        )
        self._result.parked += 1
        self._echo(f"job {job.id} parked: awaiting approval on #{pr_number}")
        self._notify(
            "queue_job_parked",
            repo=Path(job.repo).name,
            subject=f"queue job {job.id} ({job.kind} {job.scope})",
            pr=pr_number,
            detail=f"label `{RISK_APPROVED_LABEL}` or approve to resume",
            run=entry.run_id or "",
        )

    def _poll_parked(self) -> bool:
        """Re-read every due parked change request and act on what it says.

        Returns whether any job moved, so a plain drain that resumes a job on
        its very first pass keeps looping to see it through, while one that
        finds nothing new exits and leaves the parks in the store for the next
        `sdlc queue run` — the queue, not this process, is what outlives a run.

        Two things keep this inside the host's rate limits (AC4). ``poll_after``
        means a job is read once per interval no matter how fast the loop
        spins; and a job whose repo is busy, or for which no agent slot is free,
        is skipped *before* the read — there would be nothing to do with the
        answer, so the API call would be pure waste.
        """
        due = self._store.due_parked_jobs(now=self._clock())
        if not due:
            return False
        busy = self._store.running_repos()
        progressed = False
        for job in due:
            cost = agent_slots(job)
            used = self._used_slots()
            if job.repo in busy or (used and used + cost > self._config.slots):
                continue
            if job.pr_number is None:
                # A park with no CR cannot be polled. Push its next poll out so
                # it does not re-list every pass; `sdlc queue cancel/requeue`
                # are the operator's exits.
                self._store.schedule_poll(job.id, self._next_poll())
                continue
            verdict = self._approval_probe(Path(job.repo), job.pr_number)
            self._store.schedule_poll(job.id, self._next_poll())
            if verdict is None:
                # Unreadable host (offline, unauthenticated, rate-limited). Stay
                # parked and try again next interval — never guess.
                continue
            if self._act_on_verdict(job, verdict, cost=cost):
                busy.add(job.repo)
                progressed = True
        return progressed

    def _act_on_verdict(
        self, job: JobRecord, verdict: ApprovalVerdict, *, cost: int
    ) -> bool:
        """Turn one change-request verdict into the queue's next move (AC2/AC3)."""
        if verdict.state == "merged":
            # Merged by hand while we were parked. Resuming would re-enter a
            # merge stage against an already-merged CR; `sdlc reconcile` is the
            # verb that exists for exactly this and it is the one we call.
            return self._drive_parked(job, action="reconcile", cost=cost)
        if verdict.state == "closed":
            self._store.finish_job(job.id, "failed", reason="pr closed")
            self._result.failed += 1
            self._echo(f"job {job.id} failed: PR #{job.pr_number} closed without merging")
            self._announce(job, job.run_id, "failed")
            return True
        if verdict.approved:
            return self._drive_parked(
                job, action="resume", cost=cost, signal=verdict.signal
            )
        return False

    def _drive_parked(
        self, job: JobRecord, *, action: str, cost: int, signal: str = ""
    ) -> bool:
        """Take a parked job back under a lease and launch ``action`` on it."""
        claimed = self._store.take_parked_job(
            job.id, claimed_by=self._identity,
            lease_seconds=self._config.lease_seconds, now=self._clock(),
        )
        if claimed is None:
            return False  # another scheduler got there first, or the repo went busy
        launched = self._start(claimed, action=action, cost=cost)
        if launched and action == "resume":
            # Announced only once the resume is genuinely in flight — a version
            # check that blocks the job announces its own terminal instead.
            self._notify(
                "queue_job_resumed",
                repo=Path(job.repo).name,
                subject=f"queue job {job.id} ({job.kind} {job.scope})",
                pr=job.pr_number,
                signal=signal,
                run=job.run_id or "",
            )
        return True

    # --- registry ---------------------------------------------------------

    def _attach_runs(self) -> None:
        """Link each in-flight job to the run its subprocess opened.

        `run_build`/`run_fix` register with ``pid=os.getpid()``, which is
        exactly the child we spawned — so the child's pid is the join key. The
        link is what lets a killed scheduler resume rather than restart.

        The pid is not enough on its own. ``Registry.records()`` returns every
        parseable row on the host, finished ones included (it never prunes;
        ``--prune`` is a manual verb), so on a long-lived host an OS-reused pid
        can collide with a stale record from an unrelated repo — and attaching
        it would later run ``sdlc resume --run <foreign-id>`` in the wrong
        checkout. The window is widest for a job that never registers at all
        (``--dry-run``, a `fix` that aborts pre-run), where ``run_id`` stays
        ``None`` and every pass re-scans. Two more predicates close it: the
        record must belong to *this job's repo*, and it must still be open —
        our child is by definition still running when we scan.
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
            if job_id is None or record.finished_at:
                continue
            if Path(record.repo) != Path(self._in_flight[job_id].job.repo):
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

    # --- notify + shutdown ------------------------------------------------

    def _announce(self, job: JobRecord, run_id: str | None, state: str) -> None:
        """Announce a terminal job down the existing Telegram notify path (AC6).

        A *queue* event, not a second ``run_finished``: a drained job's own
        subprocess already fires ``run_finished`` for its run from
        ``finalize_run``, inheriting this process's environment and so its
        notify credentials. Reusing the event name here meant every job
        double-notified with two near-identical messages. ``queue_job_finished``
        renders through the same formatter machinery and says which job it is.
        """
        self._notify(
            "queue_job_finished",
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
    approval_probe: ApprovalProbe | None = None,
    echo: Callable[[str], None] | None = None,
    identity: str | None = None,
) -> SchedulerResult:
    """Drain the host queue: claim jobs under a lease and run them as subprocesses.

    The foreground loop behind `sdlc queue run` (Story 32.1-002). Each pass:

    1. link in-flight jobs to the runs their subprocesses registered;
    2. reap finished jobs and mirror the run's terminal status onto the job — a
       run that stopped ``AWAITING_APPROVAL`` is *parked* on its change request
       rather than ended (Story 32.2-002);
    3. renew our leases;
    4. re-read any parked change request whose poll is due and act on it: an
       approval resumes the run, a hand-merge reconciles it, a close fails the
       job;
    5. reclaim any lapsed job whose run is genuinely dead and resume it, then
       claim fresh work while agent slots and non-busy repos remain.

    Every collaborator is injectable so the loop is testable without forking:
    ``launcher`` spawns a job, ``clock``/``sleeper`` drive time, ``registry``
    supplies run liveness, ``version_check`` is Story 15.1-004's per-repo check,
    ``approval_probe`` is the read-only change-request poll, and ``notifier`` is
    the Telegram path.

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
        approval_probe=approval_probe or _default_approval_probe,
        echo=echo or print,
        identity=identity or f"{socket.gethostname()}:{os.getpid()}",
    )
    return scheduler.run()
