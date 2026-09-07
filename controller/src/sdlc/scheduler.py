# ABOUTME: The `sdlc queue run` scheduler — leased claims, per-repo exclusivity, slot cap.
# ABOUTME: Stories 32.1-002 + 32.2-002. A thin loop over queue.py + registry.py that
# ABOUTME: spawns build/fix/resume/reconcile, including the approval park and auto-resume.
# ABOUTME: Story 32.2-001 adds one host-level rate-limit window shared by every repo.

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
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence, cast

from sdlc.approval import ApprovalVerdict, poll_approval
from sdlc.queue import (
    JobBudget,
    JobRecord,
    QueuePause,
    QueueStore,
    budget_breach,
    fix_rounds_exhausted,
)
from sdlc.registry import Registry, RunRecord, pid_alive
from sdlc.risk_gate import RISK_APPROVED_LABEL

if TYPE_CHECKING:  # `build` is heavy and only needed on the rate-limit path
    from sdlc.build import Ledger

__all__ = [
    "DEFAULT_APPROVAL_POLL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_PROBE_INTERVAL_SECONDS",
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
    "ledger_fix_rounds",
    "ledger_plan_files",
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
# How often a *held* queue re-probes the live API while it waits out a window
# (Story 32.2-001). The window's own reset is the primary signal; the probe is
# the correction for an epoch that was never right — a network blip misread as a
# throttle, or a reset-less limit padded out to the full auto-wait cap. Five
# minutes is one tiny request per 300s of waiting: cheap enough to run for hours,
# frequent enough that the queue reopens minutes rather than hours late.
DEFAULT_PROBE_INTERVAL_SECONDS = 300.0

# The in-process auto-wait cap a run without a persisted reset falls back to
# (`BuildOptions.rate_limit_max_wait_s` — ~one Max rolling window). The run's own
# configured value is preferred when its ledger recorded one; this is only the
# floor for a run whose config predates the key.
_DEFAULT_RATE_LIMIT_MAX_WAIT_S = 18000

# The ledger run status that holds the whole queue. Rate-limit truth stays in
# the run's ledger (`build.py` writes it via `run_update_status`); the queue only
# caches the reset so the window is discovered once rather than once per repo.
_RATE_LIMITED = "RATE_LIMITED"

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
# ``(ledger_db_path, run_id) -> bugfix rounds burned``. A seam so the budget
# breaker is testable without standing up a ledger (Story 32.3-001).
FixRounds = Callable[[str, str], int]
# ``(ledger_db_path, run_id) -> the run's investigated files_to_modify``. The
# same shape and the same reason: the overlap graph's *write* side is testable
# without standing up a ledger (Story 32.3-001 AC2).
PlanFiles = Callable[[str, str], list[str]]
# Returns a `sdlc.capability.ProbeStatus`; typed loosely so importing this
# module never drags in the harness/capability stack.
RateLimitProbe = Callable[[], object]


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
    # Seconds between live-API re-probes while the queue waits out a rate-limit
    # window (Story 32.2-001).
    probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS


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
    # Rate-limit windows this drain waited out (Story 32.2-001) — windows, not
    # jobs: N repos sharing one closed window is still one pause.
    paused: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "started": self.started,
            "resumed": self.resumed,
            "reconciled": self.reconciled,
            "done": self.done,
            "failed": self.failed,
            "parked": self.parked,
            "paused": self.paused,
            "interrupted": self.interrupted,
        }


@dataclass
class _RateLimitPark:
    """One run's rate-limit state, read from its own ledger (Story 32.2-001).

    ``reset_at`` is the epoch the run persisted when it parked
    (``apply_rate_limit_park``); ``None`` means the limit surfaced no reset at
    all (a usage-limit with no retry-after), and the queue falls back to the
    run's configured ``max_wait_s`` — the same conservative "assume a full
    window" heuristic ``seconds_until_reset`` applies in-process.
    """

    run_id: str
    repo: str
    db: Path
    reset_at: float | None
    max_wait_s: int

    def window_until(self, now: datetime) -> datetime:
        """The instant dispatch may resume."""
        if self.reset_at is not None:
            return datetime.fromtimestamp(self.reset_at, timezone.utc)
        return now + timedelta(seconds=self.max_wait_s)

    @property
    def source(self) -> str:
        return "reset-epoch" if self.reset_at is not None else "max-wait"


@dataclass
class _InFlight:
    """One job the scheduler is currently driving."""

    job: JobRecord
    proc: JobProcess
    slots: int
    run_id: str | None
    last_renewed: datetime
    # Story 32.3-001. ``started_at`` is when *this launch* began, so a resumed
    # job gets a fresh wall clock rather than inheriting the dead scheduler's;
    # ``budget`` is the class budget frozen on the job at enqueue;
    # ``fix_rounds_baseline`` is the rounds the run had already burned when the
    # breaker last parked this job, so the cap is measured from there and a
    # requeued job is not re-parked on its first poll for a spend a human has
    # already signed off.
    started_at: datetime
    budget: JobBudget
    fix_rounds_baseline: int = 0
    # Whether this launch has already copied the run's investigated file set
    # onto the queue row (AC2). Latches, so a job's footprint is read from its
    # ledger once rather than on every poll.
    files_recorded: bool = False


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


def ledger_fix_rounds(db_path: str, run_id: str) -> int:
    """How many ``bugfix`` rounds ``run_id`` has burned, from its own ledger.

    The outer half of the two-layer retry budget (Story 32.3-001). The pipeline
    bounds each *story*'s bugfix loop from the inside with
    ``build.MAX_BUGFIX_ATTEMPTS``; this counts the same rows from the outside,
    across every story in the run, so the queue can stop paying for a job that
    is thrashing story after story while each individual loop stays legal.
    Deliberately reads the ledger rather than reaching into the pipeline's
    counters — the ledger is the run's public record.

    Counts with ``stage_attempt_count`` rather than filtering a
    ``stage_breakdown``: this runs once per in-flight job on every poll
    (``DEFAULT_POLL_SECONDS`` is 2s, so ~1800 times an hour per job), and
    materialising every stage attempt of a long run into per-attempt dicts with
    summed token counts — to then keep only the ``bugfix`` rows — is a scan and
    an allocation per poll for a number SQL can return directly.

    Degrades to ``0`` on any read failure: an unreadable ledger is a reason to
    leave the breaker un-fired, never a reason to park a healthy job or to take
    the drain down.
    """
    from sdlc.build import Ledger

    try:
        return Ledger(Path(db_path)).stage_attempt_count(run_id, "bugfix")
    except Exception:
        return 0


def ledger_plan_files(db_path: str, run_id: str) -> list[str]:
    """The files ``run_id``'s investigation said it would modify (Story 32.3-001 AC2).

    The *write* side of the queue's file-overlap graph, and the counterpart to
    :func:`ledger_fix_rounds`: both read the run's own public record rather than
    reaching into the pipeline. `fix_issue` already freezes each investigation
    plan as a ``fix-plan`` event (issue #547) precisely so a resume can recover
    it; ``files_to_modify`` is the field
    :meth:`QueueStore.overlap_holds` builds its graph from, so the queue reads
    the same frozen plan instead of asking the job subprocess to write back —
    a job stays ignorant of the queue that spawned it.

    Every plan the run recorded is unioned, earliest first, because a resumed
    run can record more than one and the job's footprint is all of them. Only
    a `fix` run records plans at all; a `build` job simply has none, which is
    the honest answer (its stories are not investigated up front) and leaves it
    a singleton in the graph.

    Degrades to ``[]`` on any read failure, and skips any plan it cannot parse:
    an unreadable ledger is a reason to leave two jobs unchained, never a reason
    to take the drain down.
    """
    from sdlc.build import Ledger
    from sdlc.fix_issue import _FIX_PLAN_SOURCE

    try:
        messages = Ledger(Path(db_path)).events_by_source(run_id, _FIX_PLAN_SOURCE)
    except Exception:
        return []
    files: set[str] = set()
    for message in messages:
        try:
            plan = json.loads(message)
        except (ValueError, TypeError):
            continue
        if isinstance(plan, dict):
            files.update(str(path) for path in (plan.get("files_to_modify") or []))
    return sorted(files)


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

@dataclass
class _ProbeContext:
    """The one attribute ``build._probe_parked_reset`` reads off its context.

    That function takes a ``_RateLimitContext`` but touches only ``.probe``;
    building a whole ``BuildOptions``/window quota here just to satisfy the type
    would be ceremony. This keeps the reuse honest and the coupling one field
    wide.
    """

    probe: RateLimitProbe | None


def _as_float(value: object) -> float | None:
    """``value`` as an epoch float, or ``None`` when it is absent/unparseable.

    Ledger config is whatever JSON was written into it, so the epoch may arrive
    as a number, as a numeric string, or (from the queue's own pause row) as an
    ISO-8601 instant. All three are the same fact; anything else is junk and
    reads as "no reset recorded".
    """
    if value is None:
        return None
    if isinstance(value, bool):  # `True` is not an epoch, whatever int() says
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None


def _as_int(value: object, fallback: int) -> int:
    """``value`` as a positive int, or ``fallback`` when it is absent/junk."""
    parsed = _as_float(value)
    if parsed is None or parsed <= 0:
        return fallback
    return int(parsed)


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
        fix_rounds: FixRounds,
        plan_files: PlanFiles,
        probe: RateLimitProbe | None,
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
        self._fix_rounds = fix_rounds
        self._plan_files = plan_files
        self._probe = probe
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
                self._record_plan_files()
                self._reap()
                self._enforce_budgets()
                self._renew()
                self._check_rate_limit()
                polled = self._poll_parked()
                progressed = self._fill_slots() or polled
                self._stamp_repo_busy()
                if (
                    not self._in_flight
                    and not progressed
                    and not self._config.follow
                    and not self._waiting_out_window()
                ):
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
        """Claim and launch while slots and claimable work remain.

        A host-level rate-limit pause (Story 32.2-001) closes this door
        entirely: the Max plan every repo shares is exhausted, so claiming *any*
        job — fresh or resumed — would only spend into a window that is already
        closed. Jobs already in flight are left alone; a run that parked itself
        is the one that knows how to wait.
        """
        if self._dispatch_paused():
            return False
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
        """Explain a job that could have run but was held back.

        Two reasons a claimable-looking job is standing still, both worth saying
        out loud in `sdlc queue list`: its repo is already busy (Story 32.1-002
        AC2), or it overlaps the files of an unfinished peer in that repo
        (Story 32.3-001 AC2). The overlap set is read separately because
        :meth:`QueueStore.peek_claimable` has already filtered those jobs out —
        by construction they are not candidates, so they would otherwise go
        unexplained.

        Story 32.1-003: a ``build`` candidate is only genuinely blocked by a
        running ``fix`` (build/build now overlaps freely), so it must not be
        stamped "repo busy" just because another build is live in its repo.
        """
        for job_id, holder in self._store.overlap_holds().items():
            reason = f"waiting on job {holder} (overlapping files)"
            job = self._store.get_job(job_id)
            if job is not None and job.state == "queued" and job.reason != reason:
                self._store.set_reason(job_id, reason)

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
            last_renewed=self._clock(), started_at=self._clock(),
            budget=job.job_budget(), fix_rounds_baseline=job.fix_rounds_baseline,
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
            if entry.run_id and self._rate_limit_park(entry.run_id) is not None:
                # Story 32.2-001: a run that parked itself on a closed window
                # exits non-zero, but it is *paused*, not finished. Stamping it
                # terminal would need a manual `sdlc queue requeue` to undo, so
                # the claim is handed back the `release_claim` way — state
                # ``running`` with an expired lease — and the reclaim path
                # resumes it through `sdlc resume` once the window reopens.
                # `_check_rate_limit` records the host pause from the same
                # ledger read on the next pass.
                self._store.release_claim(
                    job_id, claimed_by=self._identity,
                    reason="rate-limited: waiting for the shared window to reopen",
                    now=self._clock(),
                )
                self._echo(f"job {job_id} paused: rate-limited, awaiting the window")
                continue
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

    def _enforce_budgets(self) -> None:
        """Stop and park any in-flight job that has exhausted its budget (AC3).

        Runs *after* :meth:`_reap`, so a job that already exited on its own is
        never posthumously parked, and before :meth:`_renew`, so a lease is
        never extended on a job we are about to stop.

        The park is terminal (``needs_attention``) rather than a release,
        because the whole point is that no scheduler should pick this job up
        again unattended — a human decides whether the spend was worth
        continuing. `sdlc queue requeue` is that decision's exit, and for a job
        that already opened a run it comes back as a *resume*, so the spend so
        far is not thrown away.

        For that exit to work, the two ceilings have to be re-based when they
        fire, and they are re-based differently because they measure different
        things. The wall clock is per *launch*, so a resume gets a fresh one by
        construction. Fix rounds are read cumulatively off the run's ledger and
        a resume re-enters the **same** run, so the count that fired the breaker
        is still there on the next poll: the rounds burned are banked as the
        job's baseline (:meth:`QueueStore.record_fix_rounds_baseline`) and the
        cap is measured from it. A requeue then buys exactly one more class
        budget rather than an immediate re-park — or an uncapped job.

        Only the thrash arm banks a baseline. A job the clock stopped has not
        spent its rounds, and crediting them would quietly hand it a second
        allowance it never earned.
        """
        now = self._clock()
        for job_id, entry in list(self._in_flight.items()):
            burned = (
                self._run_fix_rounds(entry.run_id) if entry.run_id else 0
            )
            rounds = max(0, burned - entry.fix_rounds_baseline)
            reason = budget_breach(
                entry.budget,
                elapsed_seconds=(now - entry.started_at).total_seconds(),
                fix_rounds=rounds,
            )
            if reason is None:
                continue
            del self._in_flight[job_id]
            try:
                entry.proc.stop()
            except OSError as exc:
                self._echo(f"job {job_id}: could not stop pid {entry.proc.pid}: {exc}")
            if fix_rounds_exhausted(entry.budget, rounds):
                self._store.record_fix_rounds_baseline(job_id, burned)
            self._store.finish_job(job_id, "needs_attention", reason=reason)
            self._result.parked += 1
            self._echo(f"job {job_id} parked (needs_attention): {reason}")
            self._announce(entry.job, entry.run_id, "needs_attention")

    def _record_plan_files(self) -> None:
        """Copy each in-flight job's investigated file set onto its row (AC2).

        The production writer behind :meth:`QueueStore.overlap_holds`: without
        it the ``files`` column would stay NULL on every job and the overlap
        graph would be all singletons, so two jobs racing the same paths would
        never serialise.

        Runs right after :meth:`_attach_runs` (a job has no ledger to read until
        it has a run) and before :meth:`_reap`, so a job that finishes in this
        same pass still leaves its footprint behind — the row keeps it, which is
        what lets a requeued job be held on the strength of what it touched last
        time. ``files_recorded`` latches per launch, and an empty answer is not
        latched: the plan is frozen mid-run, so the first passes legitimately see
        nothing yet and must look again.
        """
        for job_id, entry in list(self._in_flight.items()):
            if entry.files_recorded or entry.run_id is None:
                continue
            record = self._registry_record(entry.run_id)
            if record is None:
                continue
            files = self._plan_files(record.db, entry.run_id)
            if not files:
                continue
            self._store.record_files(job_id, files)
            entry.files_recorded = True

    def _run_fix_rounds(self, run_id: str) -> int:
        """Fix rounds burned by ``run_id``, via its registry-recorded ledger."""
        record = self._registry_record(run_id)
        if record is None:
            return 0
        return self._fix_rounds(record.db, run_id)

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

        A host-level rate-limit pause (Story 32.2-001) closes this door for the
        same reason it closes :meth:`_fill_slots`: resuming an approved job
        launches an agent against the one Max window every repo shares, and that
        window is shut. The parks keep their ``poll_after`` and are re-read on
        the pass after the window reopens.
        """
        if self._dispatch_paused():
            return False
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
    # --- one rate-limit window for the whole queue (Story 32.2-001) --------

    def _check_rate_limit(self) -> None:
        """Open, hold, or lift the host-level pause — once per window.

        FX runs every repo on one Max subscription, so a closed window is a
        property of the *host*, not of the run that happened to discover it.
        Each pass therefore asks one question of the queue as a whole: is any
        live job's run parked ``RATE_LIMITED``? If so the reset is cached on the
        queue and every scheduler stops claiming until it passes — the window is
        discovered once and waited out once, with one notify for the pause and
        one for the resume rather than one per repo.

        The ledger stays the source of truth (the technical note is explicit
        about that): nothing here *decides* a rate limit, it only reads the
        verdict `build.py` already wrote and caches its reset time.
        """
        pause = self._store.dispatch_pause()
        if pause is not None:
            if pause.is_active(self._clock()) and not self._window_reopened(pause):
                return
            self._resume_dispatch(pause)
            return
        park = self._discover_rate_limit()
        if park is not None:
            self._pause_dispatch(park)

    def _dispatch_paused(self) -> bool:
        pause = self._store.dispatch_pause()
        return pause is not None and pause.is_active(self._clock())

    def _waiting_out_window(self) -> bool:
        """Whether a one-shot drain must stay alive to see the window reopen.

        Without this a plain `sdlc queue run` would exit the moment it paused,
        leaving the parked job for whoever runs the command next — the opposite
        of "the queue resumes everything itself at that moment". It only holds
        while there is work the reopened window would actually let through, so a
        pause discovered on an otherwise empty queue still exits.
        """
        if not self._dispatch_paused():
            return False
        now = self._clock()
        return bool(
            self._store.peek_claimable(now=now)
            or self._store.expired_running_jobs(now=now)
        )

    def _discover_rate_limit(self) -> "_RateLimitPark | None":
        """The first live job whose run is parked on a closed window, if any.

        Reads every ``running`` job in the *store*, not just this scheduler's
        own in-flight ones: a second `sdlc queue run` on the same host shares the
        one subscription, and a job parked by a previous drain still holds the
        window it discovered.
        """
        for job in self._store.list_jobs():
            if job.state != "running" or not job.run_id:
                continue
            park = self._rate_limit_park(job.run_id)
            if park is not None:
                return park
        return None

    def _rate_limit_park(self, run_id: str) -> "_RateLimitPark | None":
        """``run_id``'s rate-limit park as its own ledger records it, or ``None``.

        Advisory by construction: a missing registry entry, a ledger that cannot
        be opened, or a config value that is not a number all read as "not
        rate-limited". Failing a whole drain over an unreadable ledger would be
        far worse than missing one pause, which the next pass re-reads anyway.
        """
        record = self._registry_record(run_id)
        if record is None or record.finished_at:
            return None
        try:
            from sdlc.build import Ledger  # heavy module; only needed on this path

            ledger = Ledger(Path(record.db))
            row = ledger.run_row(run_id)
            if row is None or row.get("status") != _RATE_LIMITED:
                return None
            config = ledger.run_config(run_id)
        except Exception:  # noqa: BLE001 - a ledger read must never fail a drain
            return None
        return _RateLimitPark(
            run_id=run_id,
            repo=record.repo,
            db=Path(record.db),
            reset_at=_as_float(config.get("rate_limit_reset_at")),
            max_wait_s=_as_int(
                config.get("rate_limit_max_wait_s"), _DEFAULT_RATE_LIMIT_MAX_WAIT_S
            ),
        )

    def _pause_dispatch(self, park: "_RateLimitPark") -> None:
        """Cache the window on the queue and announce it — at most once."""
        now = self._clock()
        until = park.window_until(now)
        if until <= now:
            return  # the recorded reset has already passed — nothing to wait for
        detail = (
            "reset recorded by the run"
            if park.reset_at is not None
            else f"no reset time — waiting the configured {park.max_wait_s}s cap"
        )
        opened = self._store.pause_dispatch(
            until=until,
            reason=f"rate limited ({detail})",
            run_id=park.run_id,
            repo=park.repo,
            source=park.source,
            now=now,
        )
        if not opened:
            return  # another job already discovered this window — stay silent
        self._result.paused += 1
        self._echo(
            f"queue paused: rate limited (run {park.run_id[:8]}) — no job is "
            f"claimed until {until.isoformat()}"
        )
        self._notify(
            "queue_paused",
            repo=Path(park.repo).name,
            subject="development queue",
            reset_at=until.isoformat(),
            detail=detail,
            run=park.run_id,
        )

    def _resume_dispatch(self, pause: QueuePause) -> None:
        """Lift the pause and announce it — once, for the whole queue."""
        self._store.clear_pause()
        self._echo("queue resumed: the rate-limit window reopened")
        self._notify(
            "queue_resumed",
            repo=Path(pause.repo).name if pause.repo else "",
            subject="development queue",
            paused_until=pause.paused_until,
            run=pause.run_id or "",
        )

    def _window_reopened(self, pause: QueuePause) -> bool:
        """Whether a live probe says the window is open before its reset says so.

        Mirrors :func:`sdlc.build._probe_parked_reset` — reused, not
        reimplemented, down to writing the verdict into the parked run's own
        ledger so a queue-side check and a resume-side one read identically.
        Only ``AVAILABLE`` reopens; ``UNAVAILABLE`` and ``UNKNOWN`` (no probe
        wired, or the probe itself errored) keep the window shut, because this
        gate protects a quota that may still be closed and so must never fail
        open. Throttled to ``probe_interval_seconds`` and stamped on the queue,
        so a five-hour blind wait costs a handful of tiny requests rather than
        one per poll.
        """
        if self._probe is None or not self._due_for_probe(pause):
            return False
        self._store.mark_pause_probed(now=self._clock())
        from sdlc.build import _probe_parked_reset
        from sdlc.capability import ProbeStatus

        ledger = self._pause_ledger(pause)
        if ledger is None:
            return False
        reset_at = _as_float(pause.paused_until)
        try:
            status = _probe_parked_reset(
                ledger,
                pause.run_id or "",
                # Structurally all that function reads is ``.probe`` — see
                # :class:`_ProbeContext`. The cast keeps the reuse honest
                # without constructing a whole BuildOptions to satisfy a type.
                cast(Any, _ProbeContext(probe=self._probe)),
                reset_at if reset_at is not None else 0.0,
            )
        except Exception:  # noqa: BLE001 - a probe must never fail a drain
            return False
        return status is ProbeStatus.AVAILABLE

    def _due_for_probe(self, pause: QueuePause) -> bool:
        last = pause.probed_at or pause.paused_at
        try:
            since = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return True
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        elapsed = (self._clock() - since).total_seconds()
        return elapsed >= self._config.probe_interval_seconds

    def _pause_ledger(self, pause: QueuePause) -> "Ledger | None":
        """The parked run's ledger, so the probe verdict is logged where it belongs.

        ``None`` when the run is no longer discoverable — the verdict would have
        nowhere to go, and a probe whose outcome cannot be recorded is exactly
        the "no evidence" case the gate keeps the window shut for.
        """
        if not pause.run_id:
            return None
        record = self._registry_record(pause.run_id)
        if record is None:
            return None
        try:
            from sdlc.build import Ledger

            return Ledger(Path(record.db))
        except Exception:  # noqa: BLE001
            return None

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
    fix_rounds: FixRounds | None = None,
    plan_files: PlanFiles | None = None,
    probe: RateLimitProbe | None = None,
    echo: Callable[[str], None] | None = None,
    identity: str | None = None,
) -> SchedulerResult:
    """Drain the host queue: claim jobs under a lease and run them as subprocesses.

    The foreground loop behind `sdlc queue run` (Story 32.1-002). Each pass:

    1. link in-flight jobs to the runs their subprocesses registered;
    2. reap finished jobs and mirror the run's terminal status onto the job — a
       run that stopped ``AWAITING_APPROVAL`` is *parked* on its change request
       rather than ended (Story 32.2-002);
    3. stop and park any job that has exhausted its per-class budget
       (Story 32.3-001) — fix rounds read from the run's ledger, wall clock
       measured from this launch;
    4. renew our leases;
    5. open, hold or lift the one host-level rate-limit window (Story 32.2-001);
    6. re-read any parked change request whose poll is due and act on it: an
       approval resumes the run, a hand-merge reconciles it, a close fails the
       job;
    7. reclaim any lapsed job whose run is genuinely dead and resume it, then
       claim fresh work while agent slots and non-busy repos remain.

    Every collaborator is injectable so the loop is testable without forking:
    ``launcher`` spawns a job, ``clock``/``sleeper`` drive time, ``registry``
    supplies run liveness, ``version_check`` is Story 15.1-004's per-repo check,
    ``approval_probe`` is the read-only change-request poll, ``fix_rounds``
    counts a run's burned bugfix rounds, ``plan_files`` reads the file set a
    run's investigation froze (the overlap graph's write side), ``probe`` is the
    live-API rate-limit check behind a held window, and ``notifier`` is the
    Telegram path.

    Daemonisation is deliberately *not* built here: the documented path is the
    Epic-30 30.3-001 LaunchAgent pattern (KeepAlive, standard logs, secrets from
    the env/file convention) wrapping this same foreground command.
    """
    from sdlc.build import default_rate_limit_probe
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
        fix_rounds=fix_rounds or ledger_fix_rounds,
        plan_files=plan_files or ledger_plan_files,
        probe=probe if probe is not None else default_rate_limit_probe,
        echo=echo or print,
        identity=identity or f"{socket.gethostname()}:{os.getpid()}",
    )
    return scheduler.run()
