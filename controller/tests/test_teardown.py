# ABOUTME: Tests for Story 35.5-002 — a stopped controller takes its agents down with it.
# ABOUTME: Real process groups and signals; no network, no permission tricks, CI-safe as root.

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sdlc import dispatch, scheduler, teardown
from sdlc.build import Ledger
from sdlc.dispatch import dispatch_agent
from sdlc.resume import compute_resume_plan, has_resumable_work
from sdlc.teardown import StoppedByWorker, stop_guard

# Generous: a loaded CI container (2 CPUs, shared with GitLab) is slow to reap.
WAIT_S = 20.0

# An agent that starts a tool subprocess of its own, reports both pids, then idles.
AGENT = """
import os, subprocess, sys, time
tool = subprocess.Popen(["sleep", "300"])
with open(sys.argv[1], "w") as out:
    out.write(f"{os.getpid()} {tool.pid}")
time.sleep(300)
"""


@pytest.fixture(autouse=True)
def _default_stop_signal_dispositions():
    """Run each test with the interpreter's own SIGTERM/SIGINT handling.

    Another test may have left its handler in place; the guard installs only over
    the defaults, and a SIGTERM that finds nothing installed would kill pytest.
    """
    previous = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGTERM, previous[0])
    signal.signal(signal.SIGINT, previous[1])


@pytest.fixture(autouse=True)
def _no_inherited_agent_tracking(monkeypatch):
    """A suite run inside a fleet job inherits that job's tracking dir; keep it out."""
    monkeypatch.delenv(teardown.AGENT_PIDS_ENV, raising=False)


# --- helpers ---------------------------------------------------------------------


def _is_running(pid: int) -> bool:
    """True while ``pid`` runs; a zombie (nobody reaped it) counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_until(predicate, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _agent_pids(pidfile: Path) -> tuple[int, int]:
    _wait_until(lambda: pidfile.exists() and pidfile.read_text().count(" ") == 1, "agent start")
    agent, tool = pidfile.read_text().split()
    return int(agent), int(tool)


def _assert_gone(*pids: int) -> None:
    _wait_until(lambda: not any(_is_running(p) for p in pids), f"pids {pids} to die")


def _kill_leftovers(*pids: int) -> None:
    for pid in pids:
        # A pid never learned stays 0, and `getpgid(0)` is pytest's own group.
        if pid <= 0:
            continue
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def _agent_cmd(pidfile: Path) -> list[str]:
    return [sys.executable, "-c", AGENT, str(pidfile)]


def _signal_self_when_agent_is_up(pidfile: Path, sig: int) -> threading.Timer:
    def fire() -> None:
        _agent_pids(pidfile)
        os.kill(os.getpid(), sig)

    timer = threading.Timer(0.0, fire)
    timer.daemon = True
    timer.start()
    return timer


@pytest.fixture
def ledger_run(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-35", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(run_id, "35.5-002", "", "t", "Must", 3, "backend", "", None, "TODO")
    ledger.set_story_status(run_id, "35.5-002", "IN_PROGRESS")
    ledger.stage_start(run_id, "35.5-002", "bugfix", 1)
    return ledger, run_id


def _events(ledger: Ledger, run_id: str) -> list[str]:
    with sqlite3.connect(ledger.db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT message FROM events WHERE run_id = ? ORDER BY rowid", (run_id,)
        )]


# --- AC2: the controller owns teardown on SIGTERM ---------------------------------


@pytest.mark.parametrize(
    ("sig", "exit_code", "name"),
    [(signal.SIGTERM, 143, "SIGTERM"), (signal.SIGINT, 130, "SIGINT")],
)
def test_a_stop_signal_takes_the_agent_down_and_closes_the_run(
    tmp_path, ledger_run, sig, exit_code, name
) -> None:
    """SIGTERM (worker stop) and Ctrl-C (local run) tear down identically (AC2, AC4)."""
    ledger, run_id = ledger_run
    pidfile = tmp_path / "pids"
    agent = tool = 0
    try:
        with pytest.raises(SystemExit) as stopped:
            with stop_guard():
                teardown.bind_run(ledger, run_id)
                _signal_self_when_agent_is_up(pidfile, sig)
                dispatch_agent("bugfix", "go", agent_cmd=_agent_cmd(pidfile))
        agent, tool = _agent_pids(pidfile)
        _assert_gone(agent, tool)
    finally:
        _kill_leftovers(agent, tool)

    assert stopped.value.code == exit_code
    assert ledger.run_row(run_id)["status"] == "FAILED"  # closed, not left IN_PROGRESS
    events = _events(ledger, run_id)
    assert f"bugfix: stopped by worker: {name}" in events
    # The attempt stays IN_PROGRESS — a crashed attempt, which resume re-enters.
    assert ledger.in_progress_stages(run_id) == [("35.5-002", "bugfix")]


def test_the_agent_is_torn_down_through_terminate_process_group(
    tmp_path, ledger_run, monkeypatch
) -> None:
    ledger, run_id = ledger_run
    pidfile = tmp_path / "pids"
    seen: list[int] = []
    real = dispatch._terminate_process_group

    def spy(proc, **kwargs):
        seen.append(proc.pid)
        real(proc, **kwargs)

    monkeypatch.setattr(dispatch, "_terminate_process_group", spy)
    agent = tool = 0
    try:
        with pytest.raises(SystemExit):
            with stop_guard():
                teardown.bind_run(ledger, run_id)
                _signal_self_when_agent_is_up(pidfile, signal.SIGTERM)
                dispatch_agent("bugfix", "go", agent_cmd=_agent_cmd(pidfile))
        agent, tool = _agent_pids(pidfile)
        _assert_gone(agent, tool)
    finally:
        _kill_leftovers(agent, tool)
    assert seen == [agent]


def test_a_stop_unwinds_a_worker_thread_without_recording_a_failure(
    tmp_path, ledger_run
) -> None:
    """A parallel run dispatches from worker threads; the stop reaches them too."""
    ledger, run_id = ledger_run
    pidfile = tmp_path / "pids"
    outcome: list[BaseException] = []

    def worker() -> None:
        try:
            dispatch_agent("bugfix", "go", agent_cmd=_agent_cmd(pidfile))
        except BaseException as exc:  # noqa: BLE001 - the test inspects what escaped
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    agent = tool = 0
    try:
        with pytest.raises(SystemExit):
            with stop_guard():
                teardown.bind_run(ledger, run_id)
                thread.start()
                _agent_pids(pidfile)
                os.kill(os.getpid(), signal.SIGTERM)
                thread.join(WAIT_S)  # interrupted by the handler's exit
        thread.join(WAIT_S)
        agent, tool = _agent_pids(pidfile)
        _assert_gone(agent, tool)
    finally:
        _kill_leftovers(agent, tool)

    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], StoppedByWorker)
    assert not isinstance(outcome[0], Exception)  # no `except Exception` can swallow it


def test_no_agent_starts_once_a_stop_is_under_way(monkeypatch) -> None:
    monkeypatch.setattr(teardown, "_stop_reason", "SIGTERM")

    def forbidden(*_a, **_k):
        raise AssertionError("an agent was spawned during teardown")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(StoppedByWorker):
        dispatch_agent("build", "go", agent_cmd=["true"])


def test_the_guard_leaves_a_foreign_sigterm_handler_alone() -> None:
    def mine(_signum, _frame) -> None:  # pragma: no cover - never fired
        pass

    previous = signal.signal(signal.SIGTERM, mine)
    try:
        with stop_guard():
            assert signal.getsignal(signal.SIGTERM) is mine
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_the_guard_restores_the_default_handlers_and_state() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    with stop_guard():
        assert signal.getsignal(signal.SIGTERM) is teardown._on_signal
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before
    assert teardown.stop_requested() is None


def test_a_stopped_run_resumes_the_story_from_the_interrupted_stage(tmp_path) -> None:
    """AC2: the run is closed, yet `sdlc resume --run` re-enters the stage it was in."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-35", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(run_id, "35.5-002", "", "t", "Must", 3, "backend", "", None, "TODO")
    ledger.set_story_status(run_id, "35.5-002", "IN_PROGRESS")
    ledger.stage_start(run_id, "35.5-002", "build", 1)

    with stop_guard():
        teardown.bind_run(ledger, run_id)
        teardown.close_stopped_run("worker shutdown")

    assert ledger.run_row(run_id)["status"] == "FAILED"
    assert "build: stopped by worker: worker shutdown" in _events(ledger, run_id)
    story = compute_resume_plan(ledger, run_id)["35.5-002"]
    assert (story.next_stage, story.start_attempt) == ("build", 2)
    assert has_resumable_work(ledger, run_id)


@pytest.mark.parametrize(
    ("stated", "recorded"),
    [
        ("cancelled by operator", "cancelled by operator"),
        # A fleet job's controller `kill`ed by hand: the worker stated nothing.
        (None, "SIGTERM"),
    ],
)
def test_the_stop_is_recorded_under_the_worker_s_reason_else_the_signal(
    tmp_path, ledger_run, monkeypatch, stated, recorded
) -> None:
    tracking = tmp_path / "agents"
    tracking.mkdir()
    monkeypatch.setenv(teardown.AGENT_PIDS_ENV, str(tracking))
    if stated is not None:
        teardown.write_stop_reason(tracking, stated)
    ledger, run_id = ledger_run
    with pytest.raises(SystemExit):
        with stop_guard():
            teardown.bind_run(ledger, run_id)
            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                time.sleep(0.05)  # the handler's exit interrupts this
    assert f"bugfix: stopped by worker: {recorded}" in _events(ledger, run_id)


def test_a_stop_finishes_the_run_in_the_registry_too(ledger_run) -> None:
    """The dashboard sidebar reads the registry: a stopped run must not stay live there."""
    ledger, run_id = ledger_run
    finished: list[tuple[str, str, int]] = []

    class Registry:
        def mark_finished(self, rid: str, status: str, *, completed: int) -> None:
            finished.append((rid, status, completed))

    with stop_guard():
        teardown.bind_run(ledger, run_id, Registry())
        teardown.close_stopped_run("worker shutdown")
    assert finished == [(run_id, "FAILED", 0)]


def test_a_ledger_that_cannot_be_written_never_masks_the_stop() -> None:
    class LockedLedger:
        def in_progress_stages(self, _run_id: str) -> list[tuple[str, str]]:
            raise sqlite3.OperationalError("database is locked")

    with stop_guard():
        teardown.bind_run(LockedLedger(), "run-1")
        teardown.close_stopped_run("SIGTERM")  # swallowed: the teardown carries on


def test_a_second_signal_mid_teardown_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(teardown, "_stop_reason", "SIGTERM")
    teardown._on_signal(signal.SIGINT, None)  # no re-entry, no second raise
    assert teardown.stop_requested() == "SIGTERM"


# --- tracking ---------------------------------------------------------------------


def test_each_agent_is_tracked_by_pid_and_pgid_until_it_exits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(teardown.AGENT_PIDS_ENV, str(tmp_path))
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        teardown.register_agent(proc)
        assert (tmp_path / f"agent-{proc.pid}").read_text().split() == [
            str(proc.pid), str(os.getpgid(proc.pid)),
        ]
        proc.kill()
        proc.wait(WAIT_S)
        teardown.prune_finished()
        assert list(tmp_path.iterdir()) == []
    finally:
        _kill_leftovers(proc.pid)
        proc.wait(WAIT_S)


def test_a_dispatch_leaves_no_tracking_behind(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(teardown.AGENT_PIDS_ENV, str(tmp_path))
    with pytest.raises(dispatch.AgentDispatchError):
        dispatch_agent("build", "go", agent_cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    assert list(tmp_path.iterdir()) == []


# --- AC1/AC3: the scheduler's stop, end to end ------------------------------------

CONTROLLER = """
import sys
from sdlc.dispatch import dispatch_agent
from sdlc.teardown import stop_guard

with stop_guard():
    dispatch_agent("build", "go", agent_cmd=[sys.executable, "-c", {agent!r}, sys.argv[1]])
"""

# A controller that is dead before it can clean up: it spawns a tracked agent, then
# ignores SIGTERM so the scheduler has to SIGKILL it.
STUBBORN_CONTROLLER = """
import signal, subprocess, sys, time
from sdlc import teardown

signal.signal(signal.SIGTERM, signal.SIG_IGN)
agent = subprocess.Popen([sys.executable, "-c", {agent!r}, sys.argv[1]], start_new_session=True)
teardown.register_agent(agent)
time.sleep(300)
"""


def test_stopping_a_job_takes_its_controller_and_agent_down(tmp_path) -> None:
    """AC1: nothing the job dispatched survives the scheduler's stop."""
    pidfile = tmp_path / "pids"
    job = scheduler._default_launcher(
        [sys.executable, "-c", CONTROLLER.format(agent=AGENT), str(pidfile)], tmp_path
    )
    agent = tool = 0
    try:
        agent, tool = _agent_pids(pidfile)
        job.stop()
        _assert_gone(job.pid, agent, tool)
        assert job.reap_orphans() == []  # the controller already did the work
        assert job.poll() == 143
    finally:
        _kill_leftovers(job.pid, agent, tool)


# A controller with a live ledger run, so the test can read what it recorded.
LEDGER_CONTROLLER = """
import sys
from sdlc import teardown
from sdlc.build import Ledger
from sdlc.dispatch import dispatch_agent
from sdlc.teardown import stop_guard

with stop_guard():
    teardown.bind_run(Ledger(sys.argv[2]), sys.argv[3])
    dispatch_agent("build", "go", agent_cmd=[sys.executable, "-c", {agent!r}, sys.argv[1]])
"""


def test_the_ledger_records_why_the_worker_stopped_the_job(tmp_path) -> None:
    """AC2 in the fleet: the worker's reason, not just the signal, names the stop."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-35", "serial")
    ledger.story_upsert(run_id, "35.5-002", "", "t", "Must", 3, "backend", "", None, "TODO")
    ledger.stage_start(run_id, "35.5-002", "bugfix", 1)
    pidfile = tmp_path / "pids"
    job = scheduler._default_launcher(
        [
            sys.executable, "-c", LEDGER_CONTROLLER.format(agent=AGENT),
            str(pidfile), str(ledger.db_path), run_id,
        ],
        tmp_path,
    )
    agent = tool = 0
    try:
        agent, tool = _agent_pids(pidfile)
        job.note_stop_reason("budget exhausted: 5 fix rounds (cap 5)")
        job.stop()
        _assert_gone(job.pid, agent, tool)
        assert job.reap_orphans() == []
    finally:
        _kill_leftovers(job.pid, agent, tool)

    assert "bugfix: stopped by worker: budget exhausted: 5 fix rounds (cap 5)" in _events(
        ledger, run_id
    )
    assert ledger.run_row(run_id)["status"] == "FAILED"


def test_a_sigkilled_controller_leaves_nothing_after_the_backstop(tmp_path, monkeypatch) -> None:
    """AC3: the controller never got to clean up; `stop()` + the backstop reap its agent."""
    monkeypatch.setattr(scheduler, "_STOP_GRACE_SECONDS", 0.5)
    pidfile = tmp_path / "pids"
    job = scheduler._default_launcher(
        [sys.executable, "-c", STUBBORN_CONTROLLER.format(agent=AGENT), str(pidfile)], tmp_path
    )
    agent = tool = 0
    try:
        agent, tool = _agent_pids(pidfile)
        job.stop()
        _assert_gone(job.pid)
        assert _is_running(agent)  # its own session: the group kill never reached it
        assert job.reap_orphans() == [agent]
        _assert_gone(agent, tool)
    finally:
        _kill_leftovers(job.pid, agent, tool)


def test_reaping_with_nothing_tracked_is_a_no_op(tmp_path) -> None:
    assert teardown.reap_tracked_agents(tmp_path) == []
    assert teardown.reap_tracked_agents(tmp_path / "missing") == []


def test_reaping_tidies_the_worker_s_stop_reason_away(tmp_path) -> None:
    tracking = tmp_path / "agents"
    tracking.mkdir()
    teardown.write_stop_reason(tracking, "cancelled by operator")
    assert teardown.reap_tracked_agents(tracking) == []
    assert not tracking.exists()


def test_a_job_without_a_tracking_dir_takes_no_stop_reason(tmp_path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(WAIT_S)
    job = scheduler._PopenProcess(proc)
    job.note_stop_reason("worker shutdown")  # nowhere to write it: a no-op
    assert job.reap_orphans() == []


def test_a_stale_pid_file_for_a_dead_agent_reaps_nothing(tmp_path) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    gone.wait(WAIT_S)
    (tmp_path / f"agent-{gone.pid}").write_text(f"{gone.pid} {gone.pid}\n")
    (tmp_path / "agent-junk").write_text("not pids")
    assert teardown.reap_tracked_agents(tmp_path) == []
    assert not tmp_path.exists()  # tidied up


def test_the_launcher_hands_the_job_its_own_tracking_dir(tmp_path) -> None:
    out = tmp_path / "env"
    job = scheduler._default_launcher(
        [sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.environ['SDLC_AGENT_PIDS_DIR'])"],
        tmp_path,
    )
    _wait_until(lambda: job.poll() is not None and out.exists(), "job to finish")
    assert Path(out.read_text()).name.startswith("sdlc-agents-")
    assert not Path(out.read_text()).exists()  # empty dir removed once the job is gone


# --- the spawn window -------------------------------------------------------------


def test_an_agent_is_not_spawned_with_stop_signals_blocked(tmp_path) -> None:
    """A signal mask set around the spawn would be inherited and leave the agent deaf."""
    report = tmp_path / "mask"
    code = (
        "import signal, sys\n"
        "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        f"open({str(report)!r}, 'w').write(str(sorted(int(s) for s in blocked "
        "if s in (signal.SIGTERM, signal.SIGINT))))\n"
    )
    with pytest.raises(Exception):  # noqa: B017 - no result block, by design
        dispatch_agent("build", "go", agent_cmd=[sys.executable, "-c", code])
    assert report.read_text() == "[]"


def test_a_stop_during_the_spawn_window_is_replayed_once_the_agent_is_registered(
    ledger_run,
) -> None:
    ledger, run_id = ledger_run
    spawned = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        with pytest.raises(SystemExit) as stopped:
            with stop_guard():
                teardown.bind_run(ledger, run_id)
                with teardown.spawning():
                    os.kill(os.getpid(), signal.SIGTERM)
                    assert teardown.stop_requested() is None  # held back, not lost
                    teardown.register_agent(spawned)
        assert stopped.value.code == 143
        _assert_gone(spawned.pid)  # registered before the replay, so it was torn down
        assert ledger.run_row(run_id)["status"] == "FAILED"
    finally:
        _kill_leftovers(spawned.pid)
        spawned.wait(WAIT_S)


def test_cleanup_of_a_pid_never_learned_spares_the_test_runner(monkeypatch) -> None:
    """A test that fails before reading the agent's pid cleans up pid ``0``.

    ``os.getpgid(0)`` is the caller's own group: SIGKILLing it took pytest and the
    shell that launched it down silently, mid-suite, with no failure reported.
    """
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))
    _kill_leftovers(0, 0)
    assert signalled == []


def test_an_agent_registered_after_a_stop_began_ends_itself(monkeypatch) -> None:
    """A worker thread that spawns mid-teardown missed the sweep."""
    late = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        monkeypatch.setattr(teardown, "_stop_reason", "SIGTERM")
        teardown.register_agent(late)
        _assert_gone(late.pid)
    finally:
        _kill_leftovers(late.pid)
        late.wait(WAIT_S)


def test_a_non_popen_object_is_never_registered_as_an_agent() -> None:
    class Fake:
        pid = os.getpid()

    teardown.register_agent(Fake())  # must not track (or signal) a foreign pid


def test_the_pid_file_falls_back_to_the_pid_when_the_group_is_unreadable(
    tmp_path, monkeypatch
) -> None:
    def boom(_pid: int) -> int:
        raise OSError

    monkeypatch.setattr(os, "getpgid", boom)
    target = tmp_path / "agent-1"
    teardown._write_pid_file(target, 4242)
    assert target.read_text() == "4242 4242\n"


def test_an_unwritable_pid_file_never_fails_the_dispatch(tmp_path) -> None:
    teardown._write_pid_file(tmp_path / "missing-dir" / "agent-1", os.getpid())


def test_an_unreadable_handle_counts_as_exited() -> None:
    class Broken:
        def poll(self) -> int:
            raise RuntimeError

    assert teardown._has_exited(Broken())


def test_recording_a_stop_without_a_bound_run_is_a_no_op() -> None:
    teardown._bound = None
    teardown.close_stopped_run("sigterm")  # must not raise


def test_a_foreign_group_still_counts_as_alive(monkeypatch) -> None:
    def denied(_pgid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "killpg", denied)
    assert teardown._group_alive(1)


def test_a_vanished_group_is_not_alive(monkeypatch) -> None:
    def gone(_pgid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", gone)
    assert not teardown._group_alive(1)


def test_the_guard_is_inert_off_the_main_thread() -> None:
    seen: list[bool] = []

    def run() -> None:
        with stop_guard():
            seen.append(True)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=30)
    assert seen == [True]
