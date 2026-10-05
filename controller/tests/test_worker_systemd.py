# ABOUTME: Tests for the resident-worker systemd --user unit (Story 35.2-008): the unit
# ABOUTME: validates, and the argv it launches is what `sdlc queue run` actually accepts.

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.dispatch import SandboxUnavailableError
from sdlc.doctor import WORKER_UNIT, default_worker_unit, read_worker_unit
from sdlc.queue import CODEX_POOL, QueueStore
from sdlc.queue_worker import WorkerProfile, detect_worker_profile
from sdlc.scheduler import SchedulerResult

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "templates/systemd/sdlc-worker.service"
NIX = "/nix/var/nix/profiles/default/bin/nix"
HOME = "/home/fx"


def _directives() -> dict[str, list[str]]:
    """``{Key: [values…]}`` for the [Service] section; a key may repeat (Environment=)."""
    out: dict[str, list[str]] = {}
    section = None
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
            continue
        key, _, value = line.partition("=")
        if section == "[Service]":
            out.setdefault(key, []).append(value)
    return out


@pytest.fixture(scope="module")
def service() -> dict[str, list[str]]:
    return _directives()


def _exec_argv(service) -> list[str]:
    return shlex.split(service["ExecStart"][0])


def _sdlc_args(service) -> list[str]:
    argv = _exec_argv(service)
    return argv[argv.index("-c") + 1 :]


def test_the_unit_is_named_for_the_template_doctor_looks_for() -> None:
    assert TEMPLATE.name == WORKER_UNIT
    assert default_worker_unit().name == WORKER_UNIT
    assert default_worker_unit().parent.parts[-3:] == (".config", "systemd", "user")


def test_the_unit_has_the_three_sections_a_user_unit_needs() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "[Unit]\n" in text and "[Service]\n" in text and "[Install]\n" in text


def test_a_user_unit_is_wanted_by_default_target_and_orders_on_no_system_unit() -> None:
    # A user manager cannot order against system units: After=network-online.target
    # would be silently ignored, and says something the unit cannot deliver.
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "WantedBy=default.target" in text
    assert not [ln for ln in text.splitlines() if ln.startswith(("After=", "Requires="))]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is Linux-only")
def test_the_unit_passes_systemd_analyze_verify() -> None:
    # `verify` insists on an ExecStart that exists; the nix path only does on the box.
    done = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(TEMPLATE)], capture_output=True, text=True
    )
    assert "Unknown" not in done.stderr and "Invalid" not in done.stderr, done.stderr


def test_exec_start_goes_through_nix_develop_by_absolute_path(service) -> None:
    # Every tool lives in the dev shell, not on the login PATH.
    assert len(service["ExecStart"]) == 1
    argv = _exec_argv(service)
    assert argv[:4] == [NIX, "develop", "%h/.config/nix-dev-env", "-c"]


def test_worker_argv_names_dev_and_the_claude_shared_pool_only(service) -> None:
    assert _sdlc_args(service) == [
        "sdlc", "queue", "run", "--worker", "dev", "--pool", "claude-shared", "--follow",
    ]


def test_codex_shared_is_not_declared_by_the_unit(service) -> None:
    # Codex is not installed on the box: capability registration decides, not the unit.
    assert CODEX_POOL not in " ".join(_sdlc_args(service))


def test_no_sleep_inhibitor_wraps_the_worker(service) -> None:
    # `caffeinate -i` is a Mac idle-sleep guard; a server does not sleep (35.2-004's note).
    assert not {"caffeinate", "systemd-inhibit"} & set(_exec_argv(service))
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "caffeinate" in text and "systemd-inhibit" in text  # …and the header says so


def test_worker_is_restarted_on_any_exit_without_spinning(service) -> None:
    # `always`, not `on-failure`: exit 75 (controller reinstalled under it) is a clean
    # exit that must still restart on the new code.
    assert service["Restart"] == ["always"]
    assert int(service["RestartSec"][0]) >= 10


def test_systemd_waits_out_a_full_shutdown_before_sigkill(service) -> None:
    # SIGTERM takes the Ctrl-C path: each in-flight job gets SIGTERM, then SIGKILL after
    # a grace, and only then is its lease released. Cut short, a job runs on unwatched.
    from sdlc.scheduler import _STOP_GRACE_SECONDS, DEFAULT_SLOTS

    args = _sdlc_args(service)
    slots = int(args[args.index("--slots") + 1]) if "--slots" in args else DEFAULT_SLOTS
    assert int(service["TimeoutStopSec"][0]) > slots * 2 * _STOP_GRACE_SECONDS
    assert service["KillMode"] == ["mixed"]  # SIGTERM the worker alone; it stops its jobs


def test_logs_go_to_the_journal(service) -> None:
    assert service["StandardOutput"] == ["journal"] and service["StandardError"] == ["journal"]
    assert not [k for k in service if k in ("StandardOutputFile", "StandardOutPath")]


def test_path_reaches_nix_and_the_uv_tools(service) -> None:
    path = next(v for v in service["Environment"] if v.startswith("PATH=")).removeprefix("PATH=")
    entries = path.split(":")
    assert "%h/.local/bin" in entries  # sdlc, a uv tool
    assert "/nix/var/nix/profiles/default/bin" in entries
    assert "/usr/bin" in entries and "/bin" in entries


def test_git_never_prompts_and_state_is_pinned_where_an_xdg_shell_looks(service) -> None:
    env = service["Environment"]
    assert "GIT_TERMINAL_PROMPT=0" in env
    assert "XDG_STATE_HOME=%h/.local/state" in env


def test_the_token_is_an_optional_environment_file_never_baked_in(service) -> None:
    # tailscale whois is the primary identity on Linux; SDLC_QUEUE_TOKEN is only the fallback.
    assert service["EnvironmentFile"] == ["-%h/.config/sdlc/worker.env"]  # `-`: may be absent
    assert not [v for v in service["Environment"] if "TOKEN" in v.upper()]
    header = TEMPLATE.read_text(encoding="utf-8").partition("[Unit]")[0]
    assert "chmod 600" in header and "tailscale whois" in header


def test_the_header_documents_every_step_of_the_install() -> None:
    header = TEMPLATE.read_text(encoding="utf-8").partition("[Unit]")[0]
    for step in ("daemon-reload", "enable --now", "enable-linger", "journalctl --user"):
        assert step in header


def test_the_unit_parses_as_doctor_reads_it(service) -> None:
    env, argv = read_worker_unit(TEMPLATE, home=Path(HOME))
    assert env["PATH"].startswith(f"{HOME}/.local/bin:")
    assert env["XDG_STATE_HOME"] == f"{HOME}/.local/state"
    assert argv[:2] == [NIX, "develop"] and argv[2] == f"{HOME}/.config/nix-dev-env"
    assert argv[-3:] == ["--pool", "claude-shared", "--follow"]


def test_template_argv_is_accepted_by_queue_run(service, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen: dict = {}

    def fake_detect(name, *, pools=(), host=None, **_):
        seen["name"], seen["pools"] = name, list(pools)
        return WorkerProfile(name=name, host="dev-server", pools=list(pools))

    def fake_run_queue(store, **kwargs):
        seen["config"] = kwargs["config"]
        return SchedulerResult()

    monkeypatch.setattr("sdlc.queue_worker.detect_worker_profile", fake_detect)
    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)

    result = runner.invoke(app, _sdlc_args(service)[1:])

    assert result.exit_code == 0, result.output
    assert seen["name"] == "dev" and seen["pools"] == ["claude-shared"]
    assert seen["config"].follow is True  # heartbeating while idle keeps it online


# --- pools and harnesses come from what the box has (Story 35.2-008) ---------------------


def _no_runtime() -> str:
    raise SandboxUnavailableError("no container runtime")


def _profile(service, tmp_path, *, installed: set[str]) -> WorkerProfile:
    args = _sdlc_args(service)
    pools = [args[i + 1] for i, a in enumerate(args) if a == "--pool"]
    return detect_worker_profile(
        "dev",
        pools=pools,
        host="dev-server",
        work_dir=tmp_path,
        probe=lambda binary: binary in installed,
        runtime=_no_runtime,
    )


T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _register(profile: WorkerProfile, store: QueueStore, *, free: int = 2) -> None:
    profile.register_with(store, slots=2, slots_free=free, now=T0)


def _claim(store: QueueStore, worker: str):
    return store.claim_next(claimed_by=worker, lease_seconds=90, now=T0)


@pytest.fixture
def store(tmp_path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def test_dev_registers_claude_shared_and_the_claude_harness_only(service, tmp_path, store) -> None:
    _register(_profile(service, tmp_path, installed={"claude", "gh", "uv", "node"}), store)

    [worker] = store.list_workers()
    assert (worker.name, worker.host) == ("dev", "dev-server")
    assert worker.pools == ["claude-shared"]
    assert worker.harnesses == ["claude"]  # codex is advertised only once its CLI exists


def test_a_codex_stage_is_not_offered_to_dev_until_codex_exists(service, tmp_path, store) -> None:
    _register(_profile(service, tmp_path, installed={"claude"}), store)
    job_id = store.add_job(
        repo="/r/proj", kind="build", scope="s", requirements_json=json.dumps({"harness": "codex"})
    )

    assert _claim(store, "dev") is None
    store.stamp_unsatisfiable(now=T0)
    assert "no eligible worker" in (store.get_job(job_id).reason or "")


def test_a_claude_shared_job_goes_to_the_least_loaded_of_home_lab_and_dev(
    service, tmp_path, store
) -> None:
    home_lab = WorkerProfile(
        name="home-lab", host="home-lab", pools=["claude-shared"], harnesses=["claude"]
    )
    _register(home_lab, store, free=0)  # busy
    _register(_profile(service, tmp_path, installed={"claude"}), store, free=2)  # idle
    job_id = store.add_job(repo="/r/proj", kind="build", scope="s", pool="claude-shared")

    assert _claim(store, "home-lab") is None  # defers to the idler peer
    claimed = _claim(store, "dev")
    assert claimed is not None and claimed.id == job_id
