# ABOUTME: End-to-end scheduler test — real subprocesses via a PATH `sdlc` shim (32.1-002).
# ABOUTME: Proves argv construction, process-group spawn, reap, and terminal state together.

from __future__ import annotations

import json
import os
import stat
import time

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.registry import Registry
from sdlc.scheduler import SchedulerConfig, run_queue


def _shim(tmp_path, body: str) -> str:
    """A `sdlc` stand-in on PATH. `sh` only — the CI job image guarantees nothing more."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "sdlc"
    shim.write_text("#!/bin/sh\n" + body)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(bindir)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def test_a_real_subprocess_job_runs_to_a_terminal_state(tmp_path, monkeypatch) -> None:
    argv_log = tmp_path / "argv.log"
    bindir = _shim(tmp_path, f'printf "%s\\n" "$*" >> "{argv_log}"\nexit 0\n')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])

    repo = tmp_path / "alpha"
    repo.mkdir()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo=str(repo), kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--auto"]),
    )

    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=0.05),
        registry=Registry(tmp_path / "registry.json"),
        sleeper=time.sleep,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    assert result.started == 1
    assert result.done == 1
    assert store.get_job(job_id).state == "done"
    assert argv_log.read_text().strip() == "build epic-3 --auto"


def test_a_real_subprocess_that_exits_nonzero_marks_the_job_failed(
    tmp_path, monkeypatch
) -> None:
    bindir = _shim(tmp_path, "exit 3\n")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])

    repo = tmp_path / "alpha"
    repo.mkdir()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo=str(repo), kind="fix", scope="42")

    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=0.05),
        registry=Registry(tmp_path / "registry.json"),
        sleeper=time.sleep,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    assert result.failed == 1
    assert store.get_job(job_id).state == "failed"


def test_the_job_runs_in_its_own_repo_directory(tmp_path, monkeypatch) -> None:
    cwd_log = tmp_path / "cwd.log"
    bindir = _shim(tmp_path, f'pwd >> "{cwd_log}"\nexit 0\n')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])

    repo = tmp_path / "beta"
    repo.mkdir()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo=str(repo), kind="fix", scope="7")

    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=0.05),
        registry=Registry(tmp_path / "registry.json"),
        sleeper=time.sleep,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    assert cwd_log.read_text().strip() == os.path.realpath(str(repo))
