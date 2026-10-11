"""A stopped controller takes its agents down with it (Story 35.5-002).

Every dispatched agent runs in a session of its own (``start_new_session=True``,
Story 13.4-001) so a stall kill reaches its whole tool tree. The price is that a
signal aimed at the *controller's* group never reaches an agent: the scheduler's
``killpg`` on a stopped job (budget cap, cancel, shutdown) and a bare ``kill`` of
``sdlc build`` both left the agent editing a worktree nothing owned.

The controller therefore owns teardown. While :func:`stop_guard` is active, a
SIGTERM (or Ctrl-C) terminates every live agent's process group through
:func:`sdlc.dispatch._terminate_process_group`, records the interrupted stage and
closes the run in the ledger, then exits. Each agent's pid/pgid is also written to
``$SDLC_AGENT_PIDS_DIR`` (when the scheduler set one) so a scheduler that had to
SIGKILL a controller before it could clean up can still reap the survivors
(:func:`reap_tracked_agents`). The scheduler leaves *why* it stopped the job in
the same directory (:func:`write_stop_reason`), so the ledger names the budget
cap, the cancel or the shutdown rather than a bare signal.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

# The scheduler sets this on a job it launches; the controller then tracks each
# agent there, one ``agent-<pid>`` file holding ``<pid> <pgid>``.
AGENT_PIDS_ENV = "SDLC_AGENT_PIDS_DIR"
# Written there by the scheduler just before it signals the job's controller.
_STOP_REASON_FILE = "stop-reason"

# SIGTERM → SIGKILL grace for an agent being torn down, per phase. Agents are
# terminated concurrently, so the whole teardown takes at most twice this — well
# inside the scheduler's own stop grace, so the controller exits before it is
# SIGKILLed.
_AGENT_TERM_GRACE_S = 3.0

# How long the scheduler-side backstop gives an orphaned agent to die on SIGTERM
# before SIGKILL, and how often it re-checks.
_ORPHAN_TERM_GRACE_S = 3.0
_ORPHAN_POLL_S = 0.05

# Held by reference: a test may swap `subprocess.Popen` for a fake.
_POPEN = subprocess.Popen
# Re-entrant: the signal handler runs on the main thread, possibly while it holds this.
_lock = threading.RLock()
_live: dict[int, Any] = {}  # pid -> process handle of each running agent
_stop_reason: str | None = None
_spawning = False  # the main thread is between spawning an agent and registering it
_deferred: int | None = None  # a stop signal that arrived while ``_spawning``
_bound: tuple[Any, str, Any] | None = None  # (ledger, run_id, registry)


class StoppedByWorker(BaseException):
    """The controller was told to stop (SIGTERM / Ctrl-C) and its agents are down.

    A ``BaseException`` on purpose: every stage, bugfix and failure-isolation
    handler in the pipeline catches ``Exception``, and a stop must never be
    recorded as a stage failure or fed to a bugfix round — it unwinds untouched,
    like ``KeyboardInterrupt``.
    """

    def __init__(self, reason: str, exit_code: int = 143) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


# --- agent tracking -------------------------------------------------------------


def stop_requested() -> str | None:
    """The reason the controller is stopping, or ``None`` while it is not."""
    return _stop_reason


def raise_if_stopping() -> None:
    """Refuse to start or finish agent work once a stop has been requested."""
    reason = _stop_reason
    if reason is not None:
        raise StoppedByWorker(reason)


def _pid_file(pid: int) -> Path | None:
    directory = os.environ.get(AGENT_PIDS_ENV)
    return Path(directory) / f"agent-{pid}" if directory else None


def register_agent(proc: Any) -> None:
    """Track a freshly spawned agent so a stop can find it (best-effort)."""
    # Only a real child: a signal sent on the strength of any other object's `pid`
    # could land on a process that is not ours.
    if not isinstance(proc, _POPEN):
        return
    pid = proc.pid
    with _lock:
        _live[pid] = proc
    path = _pid_file(pid)
    if path is not None:
        _write_pid_file(path, pid)
    if _stop_reason is not None:
        # Spawned from a worker thread while a stop was already tearing the others
        # down: this one missed that sweep, so it ends itself.
        from sdlc.dispatch import _terminate_process_group

        _terminate_process_group(proc, grace_s=_AGENT_TERM_GRACE_S)


def _write_pid_file(path: Path, pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except (OSError, AttributeError):
        pgid = pid
    try:
        path.write_text(f"{pid} {pgid}\n")
    except OSError:
        pass  # the backstop is a second line of defence; never fail a dispatch for it


def prune_finished() -> None:
    """Forget agents that have exited, and their pid files."""
    with _lock:
        finished = {pid: p for pid, p in _live.items() if _has_exited(p)}
        for pid in finished:
            del _live[pid]
    for pid in finished:
        path = _pid_file(pid)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


def _has_exited(proc: Any) -> bool:
    try:
        return proc.poll() is not None
    except Exception:  # noqa: BLE001 - an unreadable handle is not a live agent
        return True


@contextlib.contextmanager
def spawning() -> Iterator[None]:
    """Hold a stop signal back while the main thread spawns *and* registers an agent.

    A stop landing between ``Popen`` returning and :func:`register_agent` would find
    nothing to terminate and leave that agent orphaned. The signal is replayed the
    moment the agent is registered. (Blocking the signal instead would be inherited
    by the agent across ``exec`` and leave it deaf to SIGTERM.) Another thread's
    spawn needs no hold: :func:`register_agent` sees the stop under way and ends
    the agent itself.
    """
    global _spawning, _deferred
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _spawning = True
    try:
        yield
    finally:
        _spawning = False
    pending, _deferred = _deferred, None
    if pending is not None:
        _on_signal(pending, None)


def terminate_agents(grace_s: float = _AGENT_TERM_GRACE_S) -> None:
    """Terminate every live agent's process group, concurrently."""
    from sdlc.dispatch import _terminate_process_group

    with _lock:
        procs = list(_live.values())
    threads = [
        threading.Thread(
            target=_terminate_process_group, args=(proc,), kwargs={"grace_s": grace_s},
            daemon=True,
        )
        for proc in procs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    prune_finished()


# --- run close-out ---------------------------------------------------------------


def bind_run(ledger: Any, run_id: str, registry: Any = None) -> None:
    """Name the run a stop must close; called as each run goes live."""
    global _bound
    _bound = (ledger, run_id, registry)


def close_stopped_run(reason: str) -> None:
    """Record every interrupted stage and close the bound run (never ``IN_PROGRESS``).

    The stage rows are left ``IN_PROGRESS`` — exactly a crashed attempt, which is
    what `sdlc resume` re-enters from — and the run is closed ``FAILED``, which an
    explicit ``sdlc resume --run`` (what the scheduler issues) still picks up.
    Best-effort: a ledger that cannot be written must not mask the stop itself.
    """
    bound = _bound
    if bound is None:
        return
    ledger, run_id, registry = bound
    try:
        for story_id, stage in ledger.in_progress_stages(run_id):
            ledger.event_log(
                run_id, story_id, "warn", "controller",
                f"{stage}: stopped by worker: {reason}",
            )
        ledger.event_log(
            run_id, "", "warn", "controller",
            f"run stopped by worker: {reason} — `sdlc resume --run {run_id[:8]}` "
            "continues it from the interrupted stage",
        )
        ledger.run_update_status(run_id, "FAILED")
    except Exception:  # noqa: BLE001 - never mask the stop itself
        return
    if registry is not None:
        from sdlc.build import _registry_finish

        row = ledger.run_row(run_id) or {}
        _registry_finish(registry, run_id, "FAILED", int(row.get("completed") or 0))


# --- the handler -----------------------------------------------------------------


def write_stop_reason(pids_dir: Path, reason: str) -> None:
    """Scheduler side: tell a job's controller why it is about to be stopped."""
    with contextlib.suppress(OSError):
        (pids_dir / _STOP_REASON_FILE).write_text(reason)


def _reason_for(signum: int) -> str:
    """The worker's stated reason for this stop, else the signal's own name."""
    directory = os.environ.get(AGENT_PIDS_ENV)
    if directory:
        try:
            stated = (Path(directory) / _STOP_REASON_FILE).read_text().strip()
        except OSError:
            stated = ""
        if stated:
            return stated
    return signal.Signals(signum).name


def _on_signal(signum: int, _frame: Any) -> None:
    global _stop_reason, _deferred
    if _stop_reason is not None:
        return  # teardown already under way; a second signal must not re-enter it
    if _spawning:
        _deferred = signum  # replayed once the agent being spawned is registered
        return
    reason = _reason_for(signum)
    _stop_reason = reason
    terminate_agents()
    close_stopped_run(reason)
    raise StoppedByWorker(reason, exit_code=128 + signum)


@contextlib.contextmanager
def stop_guard() -> Iterator[None]:
    """Own teardown for a controller run: SIGTERM and Ctrl-C take its agents down.

    Exits the process with ``128 + signal`` once the agents are down and the run is
    closed. Handlers are installed only where the interpreter's default is in force
    (so an embedding host's own handler wins) and only from the main thread; they,
    and the module's stop state, are restored on the way out.
    """
    global _stop_reason, _bound, _spawning, _deferred
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _stop_reason, _bound, _spawning, _deferred = None, None, False, None
    installed: dict[signal.Signals, Any] = {}
    for sig, default in (
        (signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.default_int_handler),
    ):
        previous = signal.getsignal(sig)
        if previous == default:
            signal.signal(sig, _on_signal)
            installed[sig] = previous
    try:
        yield
    except StoppedByWorker as stop:
        raise SystemExit(stop.exit_code) from None
    finally:
        for sig, previous in installed.items():
            signal.signal(sig, previous)
        _stop_reason, _bound, _spawning, _deferred = None, None, False, None


# --- scheduler-side backstop -----------------------------------------------------


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, but not ours to signal — still alive
    return True


def reap_tracked_agents(
    pids_dir: Path, *, grace_s: float = _ORPHAN_TERM_GRACE_S
) -> list[int]:
    """Kill any agent left in ``pids_dir``'s pid files; return the pids reaped.

    The backstop for a controller that was SIGKILLed (or crashed) before
    :func:`stop_guard` could tear its agents down: they have been re-parented to
    init, but each one's session id is on file. A process group cannot be
    recycled while any member lives, so a group that still answers ``killpg(0)``
    is the agent's own.
    """
    victims: list[tuple[int, int]] = []
    try:
        entries = sorted(pids_dir.glob("agent-*"))
    except OSError:
        return []
    for entry in entries:
        try:
            pid_text, pgid_text = entry.read_text().split()
            pid, pgid = int(pid_text), int(pgid_text)
        except (OSError, ValueError):
            pid = pgid = 0
        if pgid > 1 and _group_alive(pgid):
            victims.append((pid, pgid))
        with contextlib.suppress(OSError):
            entry.unlink()
    for _, pgid in victims:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    pending = [pgid for _, pgid in victims]
    while pending and time.monotonic() < deadline:
        pending = [pgid for pgid in pending if _group_alive(pgid)]
        if pending:
            time.sleep(_ORPHAN_POLL_S)
    for pgid in pending:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        (pids_dir / _STOP_REASON_FILE).unlink()
    with contextlib.suppress(OSError):
        pids_dir.rmdir()
    return [pid for pid, _ in victims]
