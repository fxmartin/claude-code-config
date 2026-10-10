# ABOUTME: Tests for a fleet worker's non-interactive git access (Story 35.2-006): the forge CLI's
# ABOUTME: credential helper, a refusal when it cannot authenticate, and a watchdog for stalled syncs.

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from sdlc import queue_worker
from sdlc.doctor import check_forge_credentials
from sdlc.queue import QueueStore, job_needs
from sdlc.queue_worker import (
    ForgeUnauthenticated,
    ForgeUnavailable,
    RepoRefused,
    SyncStalled,
    WorkerProfile,
    detect_worker_profile,
    forge_credential_ok,
    forge_git_access,
    prepare_repo,
)
from sdlc.registry import Registry
from sdlc.scheduler import SchedulerConfig, run_queue

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, check=True)


def _http_clone(where: Path, url: str) -> Path:
    """A real clone whose ``origin`` is ``url`` — no network is touched to make it."""
    where.mkdir(parents=True)
    _git(where, "init", "-q", "-b", "main")
    _git(where, "remote", "add", "origin", url)
    return where


def _job(tmp_path: Path, repo: Path, origin: str | None):
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo=str(repo), kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": origin}) if origin else None,
    )
    job = store.get_job(job_id)
    assert job is not None
    return job


def _fake_cli(directory: Path, name: str, body: str) -> None:
    """A stand-in forge CLI on PATH: only ``sh`` is assumed, as in a CI job container."""
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def no_forge_login(tmp_path, monkeypatch) -> Path:
    """`glab`/`gh` that are installed but logged in nowhere: the helper yields nothing."""
    bin_dir = tmp_path / "bin"
    for cli in ("glab", "gh"):
        _fake_cli(bin_dir, cli, "exit 1")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


# --- helper selection by origin host -------------------------------------------


def test_a_gitlab_origin_gets_the_glab_helper_and_no_interactive_prompt() -> None:
    access = forge_git_access("https://gitlab.test/root/proj.git")

    assert access is not None
    assert (access.cli, access.host, access.scheme) == ("glab", "gitlab.test", "https")
    # Cleared first, so an inherited `osxkeychain` helper can never be asked.
    assert access.config == [
        "-c", "credential.helper=",
        "-c", "credential.helper=!glab auth git-credential",
        "-c", "core.askPass=",
    ]
    assert access.env["GIT_TERMINAL_PROMPT"] == "0"
    assert access.env["GITLAB_HOST"] == "https://gitlab.test"


def test_a_github_origin_gets_the_gh_helper() -> None:
    access = forge_git_access("https://github.com/fxmartin/proj.git")

    assert access is not None
    assert access.cli == "gh"
    assert access.config[3] == "credential.helper=!gh auth git-credential"
    assert access.env["GH_HOST"] == "github.com"
    assert "GITLAB_HOST" not in access.env


def test_a_plaintext_gitlab_gets_the_private_glab_config_the_issue_host_uses() -> None:
    access = forge_git_access("http://gitlab.test/root/proj.git")

    assert access is not None
    assert access.env["GITLAB_HOST"] == "http://gitlab.test"
    assert Path(access.env["GLAB_CONFIG_DIR"], "config.yml").is_file()


def test_a_credential_in_the_origin_never_reaches_the_helper_selection() -> None:
    access = forge_git_access("https://oauth2:s3cret@gitlab.test/root/proj.git")

    assert access is not None and access.host == "gitlab.test"
    assert "s3cret" not in " ".join([*access.config, *access.env.values()])


@pytest.mark.parametrize(
    "origin", ["git@gitlab.test:root/proj.git", "ssh://git@gitlab.test/root/proj.git", "/srv/p.git"]
)
def test_an_origin_that_is_not_http_is_left_to_git(origin: str) -> None:
    assert forge_git_access(origin) is None


def test_the_fetch_runs_under_the_forge_helper_and_a_local_one_does_not(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    stall_limits: list[float | None] = []

    def fake(argv, *, env, stall_seconds=None, **_kwargs):
        calls.append((list(argv), env))
        stall_limits.append(stall_seconds)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(queue_worker, "_run_group", fake)
    remote = _http_clone(tmp_path / "remote", "https://gitlab.test/root/proj.git")
    local = _http_clone(tmp_path / "local", str(tmp_path / "bare.git"))

    queue_worker._git(remote, "fetch", "origin")
    queue_worker._git(local, "fetch", "origin")
    queue_worker._git(remote, "rev-parse", "HEAD")

    (remote_argv, remote_env), (local_argv, local_env), (plain_argv, _) = calls
    assert remote_argv[:7] == [
        "git", "-C", str(remote),
        "-c", "credential.helper=", "-c", "credential.helper=!glab auth git-credential",
    ]
    assert remote_argv[-3:] == ["fetch", "--progress", "origin"]
    assert remote_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "-c" not in local_argv and local_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "-c" not in plain_argv  # local plumbing never needs the forge
    # Network calls get the default silence watchdog; local plumbing only the timeout.
    assert stall_limits[:2] == [None, None] and stall_limits[2] == float("inf")


# --- unauthenticated: refused within seconds, not hung --------------------------


class _Unauthorized(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server's contract
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="forge"')
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def unauthorized_forge():
    server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_a_fetch_the_cli_cannot_authenticate_is_refused_quickly_naming_the_host(
    tmp_path, unauthorized_forge, no_forge_login
) -> None:
    url = f"http://{unauthorized_forge}/root/proj.git"
    clone = _http_clone(tmp_path / "Work" / "proj", url)

    started = time.monotonic()
    with pytest.raises(ForgeUnauthenticated) as refusal:
        prepare_repo(_job(tmp_path, clone, url), work_dir=tmp_path / "Work")

    assert time.monotonic() - started < 30
    assert refusal.value.retryable is True
    assert isinstance(refusal.value, ForgeUnavailable)
    assert (refusal.value.host, refusal.value.cli) == (unauthorized_forge, "glab")
    assert f"cannot authenticate to {unauthorized_forge} (glab auth login" in refusal.value.reason


@pytest.mark.parametrize("via", ["GIT_ASKPASS", "core.askPass", "SSH_ASKPASS"])
def test_an_inherited_askpass_program_is_never_asked(
    tmp_path, unauthorized_forge, no_forge_login, monkeypatch, via
) -> None:
    """git asks an askpass program *before* it honours `GIT_TERMINAL_PROMPT=0`, and one
    inherited from an IDE terminal or a gitconfig is a dialog nobody at the worker sees."""
    asked = tmp_path / "asked"
    _fake_cli(no_forge_login, "askpass", f'echo "$1" >> "{asked}"\necho nope')
    askpass = str(no_forge_login / "askpass")
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    if via == "core.askPass":
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text(f"[core]\n\taskPass = {askpass}\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    else:
        monkeypatch.setenv(via, askpass)
    url = f"http://{unauthorized_forge}/root/proj.git"
    clone = _http_clone(tmp_path / "Work" / "proj", url)

    with pytest.raises(ForgeUnauthenticated):
        prepare_repo(_job(tmp_path, clone, url), work_dir=tmp_path / "Work")

    assert not asked.exists()


def test_a_clone_the_cli_cannot_authenticate_is_refused_and_leaves_nothing(
    tmp_path, unauthorized_forge, no_forge_login
) -> None:
    url = f"http://{unauthorized_forge}/root/proj.git"
    target = tmp_path / "Work" / "proj"

    with pytest.raises(ForgeUnauthenticated):
        prepare_repo(_job(tmp_path, target, url), work_dir=tmp_path / "Work")

    assert not target.exists()


def test_the_refusal_names_the_worker_when_the_scheduler_has_one() -> None:
    refusal = ForgeUnauthenticated("gitlab.test", "glab", "https", worker="m3max")

    assert refusal.reason == (
        "worker m3max cannot authenticate to gitlab.test (glab auth login --hostname gitlab.test)"
    )


# --- stall watchdog -------------------------------------------------------------


def _gone(pid: int) -> bool:
    """Dead, or a zombie: an orphan's zombie lingers where PID 1 does not reap (a CI job
    container's bare `sh`), and `kill(pid, 0)` still succeeds on one."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False  # no procfs (macOS), where launchd reaps orphans promptly
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"


def test_a_process_that_goes_quiet_is_killed_with_its_whole_group(tmp_path, monkeypatch) -> None:
    grandchild = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(grandchild)!r}, 'w').write(str(p.pid))\n"
        "print('hello', flush=True)\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    # Headroom: the pid file is written before the first output, and an interpreter can
    # take seconds to start on a loaded CI host.
    with pytest.raises(SyncStalled):
        queue_worker._run_group([sys.executable, "-c", code], env=dict(os.environ),
                                timeout=120, stall_seconds=5.0)

    assert time.monotonic() - started < 60
    pid = int(grandchild.read_text())
    deadline = time.monotonic() + 30  # headroom: a loaded CI host reaps slowly
    while time.monotonic() < deadline:
        if _gone(pid):
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail("the stalled process's child survived the group kill")


def test_local_plumbing_is_held_to_the_timeout_not_the_silence_limit(tmp_path, monkeypatch) -> None:
    """Only a network call can be parked behind a credential; a checkout or merge on a big
    tree is silent on a pipe while it works, and must not read as a stalled sync."""
    monkeypatch.setattr(queue_worker, "_STALL_SECONDS", 0.5)
    repo = _http_clone(tmp_path / "proj", "https://gitlab.test/root/proj.git")
    quiet = f"!{shlex.quote(sys.executable)} -c 'import time; time.sleep(2)'"

    done = queue_worker._git(repo, "-c", f"alias.quiet={quiet}", "quiet")

    assert done.returncode == 0


def test_a_process_that_keeps_talking_is_not_stalled() -> None:
    code = "import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.25)\n"

    done = queue_worker._run_group([sys.executable, "-c", code], env=dict(os.environ),
                                   timeout=60, stall_seconds=1.5)

    assert done.returncode == 0
    assert done.stdout.split() == [str(i) for i in range(8)]


def test_the_overall_timeout_still_applies_to_a_chatty_process() -> None:
    code = "import time\nwhile True:\n    print('.', flush=True)\n    time.sleep(0.1)\n"

    with pytest.raises(subprocess.TimeoutExpired):
        queue_worker._run_group([sys.executable, "-c", code], env=dict(os.environ),
                                timeout=1.0, stall_seconds=30)


def test_a_stalled_fetch_is_refused_as_a_stalled_sync(tmp_path, monkeypatch) -> None:
    clone = _http_clone(tmp_path / "Work" / "proj", "https://gitlab.test/root/proj.git")

    def stall(argv, **_kwargs):
        if "fetch" in argv:
            raise SyncStalled(60)
        return subprocess.run(argv, capture_output=True, text=True, env=_GIT_ENV)

    monkeypatch.setattr(queue_worker, "_run_group", stall)
    with pytest.raises(ForgeUnavailable) as refusal:
        prepare_repo(
            _job(tmp_path, clone, "https://gitlab.test/root/proj.git"), work_dir=tmp_path / "Work"
        )

    assert refusal.value.reason == "repo sync stalled"
    assert refusal.value.retryable is True


def test_a_stalled_fetch_stops_renewing_the_lease_before_it_is_refused(tmp_path, monkeypatch) -> None:
    """The keepalive renews the job's lease while git works; a stalled sync must not keep
    renewing it, or the queue shows a healthy `running` job the way the Keychain hang did."""
    origin = "https://gitlab.test/root/proj.git"
    clone = _http_clone(tmp_path / "Work" / "proj", origin)
    monkeypatch.setattr(queue_worker, "_KEEPALIVE_SECONDS", 0.01)
    renewed = threading.Event()

    def stall(argv, **_kwargs):
        if "fetch" in argv:
            assert renewed.wait(30), "no keepalive while the fetch was in flight"
            raise SyncStalled(60)
        return subprocess.run(argv, capture_output=True, text=True, env=_GIT_ENV)

    monkeypatch.setattr(queue_worker, "_run_group", stall)
    with pytest.raises(ForgeUnavailable) as refusal:
        prepare_repo(_job(tmp_path, clone, origin), work_dir=tmp_path / "Work",
                     keepalive=renewed.set)

    assert refusal.value.reason == "repo sync stalled"
    # Joined before the refusal surfaces, so nothing can renew the lease after it.
    assert [t for t in threading.enumerate() if t.name == "sdlc-sync-keepalive"] == []


def test_a_stalled_clone_is_refused_and_its_half_clone_removed(tmp_path, monkeypatch) -> None:
    target = tmp_path / "Work" / "proj"

    def stall(argv, **_kwargs):
        (target / ".git").mkdir(parents=True)
        raise SyncStalled(60)

    monkeypatch.setattr(queue_worker, "_run_group", stall)
    with pytest.raises(ForgeUnavailable) as refusal:
        prepare_repo(
            _job(tmp_path, target, "https://gitlab.test/root/proj.git"), work_dir=tmp_path / "Work"
        )

    assert refusal.value.reason == "repo sync stalled"
    assert not target.exists()


# --- the scheduler: refuse to queued, flag the host, stop being offered the job --


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, argv, cwd):
        self.calls.append(str(cwd))
        raise AssertionError("a refused sync must never launch")


def _drain_once(tmp_path, store, preparer, *, clock=None, follow=False, sleeper=None):
    clock = clock or _Clock()
    return run_queue(
        store,
        config=SchedulerConfig(
            slots=1, poll_seconds=1.0, follow=follow,
            worker=WorkerProfile(name="m3max", host="macbook-pro-m3-max"),
        ),
        registry=Registry(tmp_path / "registry.json"),
        launcher=_Launcher(),
        clock=clock,
        sleeper=sleeper or clock.advance,
        notifier=lambda *a, **k: None,
        version_check=lambda _root: None,
        echo=lambda _line: None,
        identity="m3max",
        prepare_repo=preparer,
    )


def _refusing_with(error: Exception):
    def prepare(job, **_kwargs):
        raise error

    return prepare


def test_an_unauthenticated_sync_requeues_the_job_with_the_reason_and_flags_the_host(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(queue_worker, "forge_credential_ok", lambda *a, **k: False)
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo=str(tmp_path / "proj"), kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )

    _drain_once(
        tmp_path, store,
        _refusing_with(ForgeUnauthenticated("gitlab.test", "glab", "https")),
    )

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by) == ("queued", None)
    assert job.reason is not None
    assert job.reason.startswith("worker m3max cannot authenticate to gitlab.test (glab auth login")
    worker = store.get_worker("m3max")
    assert worker is not None and worker.forges == {"gitlab.test": False}
    # The matcher stops offering this worker jobs on that forge.
    assert store.claimable_for_worker("m3max", [job]) == []


def test_a_login_made_since_clears_the_flag_on_the_next_beat(tmp_path, monkeypatch) -> None:
    logged_in = {"now": False}
    monkeypatch.setattr(queue_worker, "forge_credential_ok", lambda *a, **k: logged_in["now"])
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(
        repo=str(tmp_path / "proj"), kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )
    flags: list[dict[str, bool]] = []
    clock = _Clock()
    calls = {"n": 0}

    def prepare(job, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ForgeUnauthenticated("gitlab.test", "glab", "https")
        raise RepoRefused("held for the test", retryable=True)

    def sleeper(_seconds: float) -> None:
        worker = store.get_worker("m3max")
        assert worker is not None
        flags.append(dict(worker.forges))
        logged_in["now"] = True  # FX ran `glab auth login` meanwhile
        clock.advance(301)  # past the heartbeat interval, the sync retry and the recheck wait
        if len(flags) >= 3:
            raise KeyboardInterrupt

    _drain_once(tmp_path, store, prepare, clock=clock, follow=True, sleeper=sleeper)

    assert flags[0] == {"gitlab.test": False}
    assert flags[-1].get("gitlab.test") is not False


def test_a_stalled_sync_requeues_the_job_with_its_lease_released(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo=str(tmp_path / "proj"), kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )

    _drain_once(tmp_path, store, _refusing_with(ForgeUnavailable("repo sync stalled")))

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by, job.lease_until) == ("queued", None, None)
    assert job.reason == "repo sync stalled"


# --- the registry, the matcher and the capability flag ---------------------------


def test_the_forge_flags_ride_the_registration(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    profile = WorkerProfile(
        name="m3max", host="m3", forges={"gitlab.test": True, "github.com": False}
    )

    profile.register_with(store, slots=1, slots_free=1)

    worker = store.get_worker("m3max")
    assert worker is not None
    assert worker.forges == {"gitlab.test": True, "github.com": False}
    assert worker.to_dict()["forges"] == {"gitlab.test": True, "github.com": False}


def test_a_job_on_a_forge_a_worker_lacks_the_credential_for_is_not_offered_to_it(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.register_worker("m3max", host="m3", forges={"gitlab.test": False})
    store.register_worker("xps", host="xps", forges={"gitlab.test": True})
    job_id = store.add_job(
        repo="/x/proj", kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )
    job = store.get_job(job_id)
    assert job is not None

    assert store.claimable_for_worker("m3max", [job]) == []
    assert [j.id for j in store.claimable_for_worker("xps", [job])] == [job_id]


def test_a_worker_that_never_reported_a_forge_is_still_offered_its_jobs(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.register_worker("old", host="old")
    job_id = store.add_job(
        repo="/x/proj", kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )
    job = store.get_job(job_id)
    assert job is not None

    assert [j.id for j in store.claimable_for_worker("old", [job])] == [job_id]


def test_nobody_able_to_authenticate_is_stamped_on_the_job(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.register_worker("m3max", host="m3", forges={"gitlab.test": False})
    job_id = store.add_job(
        repo="/x/proj", kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": "https://gitlab.test/root/proj.git"}),
    )

    store.stamp_unsatisfiable()

    job = store.get_job(job_id)
    assert job is not None and "forge credential for gitlab.test" in (job.reason or "")


def test_only_http_origins_need_a_forge_credential(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    labels = []
    for origin in ("git@gitlab.test:root/p.git", "/srv/p.git"):
        job_id = store.add_job(
            repo="/x/p", kind="build", scope="s",
            requirements_json=json.dumps({"origin": origin}),
        )
        job = store.get_job(job_id)
        assert job is not None
        labels += [label for label, _ in job_needs(job)]

    assert labels == []


def test_a_workers_forges_survive_a_pre_existing_queue_db(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "queue.db"
    store = QueueStore(path)
    store.init()
    with sqlite3.connect(path) as conn:  # a queue.db written before the column existed
        conn.execute("ALTER TABLE workers DROP COLUMN forges")
        conn.execute("DELETE FROM _migrations WHERE version = 15")

    QueueStore(path).ensure_migrated()
    QueueStore(path).register_worker("w", host="h", forges={"gitlab.test": True})

    worker = QueueStore(path).get_worker("w")
    assert worker is not None and worker.forges == {"gitlab.test": True}


# --- detection and the doctor finding --------------------------------------------


def test_the_profile_advertises_each_forge_its_clones_sit_on(tmp_path) -> None:
    work = tmp_path / "Work"
    _http_clone(work / "a", "https://gitlab.test/root/a.git")
    _http_clone(work / "b", "https://gitlab.test/root/b.git")
    _http_clone(work / "c", "https://github.com/fxmartin/c.git")
    _http_clone(work / "d", str(tmp_path / "bare.git"))
    probed: list[tuple[str, str]] = []

    def probe_forge(host: str, scheme: str) -> bool:
        probed.append((host, scheme))
        return host == "gitlab.test"

    profile = detect_worker_profile(
        "m3max", work_dir=work, probe=lambda _b: False, runtime=lambda: "podman",
        forge_probe=probe_forge,
    )

    assert profile.forges == {"gitlab.test": True, "github.com": False}
    assert sorted(probed) == [("github.com", "https"), ("gitlab.test", "https")]


def test_credential_probe_asks_the_cli_exactly_what_git_would(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    _fake_cli(
        bin_dir, "glab",
        'read -r a; read -r b\n'
        '[ "$1 $2 $3" = "auth git-credential get" ] || exit 2\n'
        '[ "$b" = "host=gitlab.test" ] || exit 1\n'
        'printf "username=oauth2\\npassword=tok\\n"',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    assert forge_credential_ok("gitlab.test", "https") is True
    assert forge_credential_ok("other.test", "https") is False


def test_a_login_made_after_the_worker_started_reaches_a_plaintext_gitlab_probe(
    tmp_path, monkeypatch
) -> None:
    """The resident worker outlives any login, so the private glab config a plaintext
    instance needs must carry a token written since the first probe — the 5-minute
    re-check and the fetch both read it through :func:`forge_git_access`."""
    from sdlc import issue_host

    monkeypatch.setattr(issue_host, "_GLAB_HTTP_CONFIG_DIRS", {})
    user_config = tmp_path / "glab-cli"
    user_config.mkdir()
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(user_config))

    def glab(argv, *, input, env, **_kwargs):  # answers from the config it is pointed at
        host = dict(line.split("=", 1) for line in input.splitlines() if "=" in line)["host"]
        config = yaml.safe_load(Path(env["GLAB_CONFIG_DIR"], "config.yml").read_text())
        token = (config["hosts"].get(host) or {}).get("token")
        out = f"username=oauth2\npassword={token}\n" if token else ""
        return subprocess.CompletedProcess(argv, 0 if token else 1, out, "")

    assert forge_credential_ok("gitlab.test", "http", runner=glab) is False
    (user_config / "config.yml").write_text("hosts:\n  gitlab.test:\n    token: fresh\n")

    assert forge_credential_ok("gitlab.test", "http", runner=glab) is True


def test_credential_probe_is_false_without_the_cli_or_when_it_hangs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert forge_credential_ok("gitlab.test", "https") is False

    def hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert forge_credential_ok("gitlab.test", "https", runner=hangs) is False


def test_doctor_reports_each_forge_credential_on_a_worker(tmp_path) -> None:
    work = tmp_path / "Work"
    _http_clone(work / "a", "https://gitlab.test/root/a.git")
    _http_clone(work / "c", "https://github.com/fxmartin/c.git")
    plist = tmp_path / "worker.plist"
    plist.write_bytes(b"<plist/>")

    finding = check_forge_credentials(
        agent_path=plist, work_dir=work, probe=lambda host, scheme: host == "gitlab.test"
    )

    assert finding is not None
    assert finding.status == "FAIL"
    assert "gitlab.test" in finding.detail and "github.com" in finding.detail
    assert "gh auth login --hostname github.com" in finding.remedy

    clean = check_forge_credentials(
        agent_path=plist, work_dir=work, probe=lambda host, scheme: True
    )
    assert clean is not None and clean.status == "CLEAN"


def test_doctor_stays_quiet_on_a_machine_with_no_worker(tmp_path) -> None:
    assert check_forge_credentials(
        agent_path=tmp_path / "absent.plist", work_dir=tmp_path, probe=lambda *_: False
    ) is None
