# ABOUTME: Tests for the resident-worker LaunchAgent template (Story 35.2-004): the plist
# ABOUTME: validates, and the argv it launches is what `sdlc queue run` actually accepts.

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.doctor import WORKER_LABEL, default_worker_plist
from sdlc.queue import default_queue_path
from sdlc.queue_worker import WorkerProfile
from sdlc.registry import default_registry_path
from sdlc.scheduler import SchedulerResult

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "templates/launchd/com.fxmartin.sdlc-worker.plist"
DASHBOARD_TEMPLATE = REPO_ROOT / "templates/launchd/com.fxmartin.sdlc-dashboard.plist"
HOME = "/Users/fx"
TAILNET_IP = "100.64.0.9"


@pytest.fixture(scope="module")
def plist() -> dict:
    with TEMPLATE.open("rb") as fh:
        return plistlib.load(fh)


def _rendered(plist: dict) -> dict:
    """The plist as the header's install `sed` writes it for user ``fx``."""
    text = plistlib.dumps(plist).decode()
    text = text.replace("__HOME__", HOME).replace("__USER__", "fx").replace("__TAILNET_IP__", TAILNET_IP)
    return plistlib.loads(text.encode())


def test_template_is_a_valid_plist(plist) -> None:
    assert plist["Label"] == "com.fxmartin.sdlc-worker"
    assert TEMPLATE.name == f"{plist['Label']}.plist"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_template_passes_plutil_lint() -> None:
    subprocess.run(["plutil", "-lint", str(TEMPLATE)], check=True, capture_output=True)


def test_doctor_looks_for_the_templates_label(plist) -> None:
    # `sdlc doctor` finds the agent by file name, and its remedy addresses it by label.
    assert plist["Label"] == WORKER_LABEL
    assert default_worker_plist().name == TEMPLATE.name


def test_the_install_sed_fills_every_placeholder() -> None:
    # Same shape as 35.1-003: the header's `sed` must cover every placeholder in
    # the body, or the rendered agent would exec a literal `__HOME__/.local/bin/sdlc`.
    header, _, body = TEMPLATE.read_text(encoding="utf-8").partition("-->")
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", header))
    assert substituted
    assert set(re.findall(r"__[A-Z_]+__", body)) <= substituted


def test_worker_starts_at_login_and_is_restarted_on_exit(plist) -> None:
    # A LaunchAgent loads at login, not boot. KeepAlive restarts the worker after
    # it exits 75 on a controller upgrade.
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 10  # a crash loop must not spin


def test_launchd_waits_out_a_full_shutdown_before_sigkill(plist) -> None:
    # `launchctl bootout` and `kickstart -k` stop the worker with SIGTERM, which
    # takes the Ctrl-C path: one in-flight job after another gets SIGTERM, then
    # SIGKILL after a grace, and only then is its lease released. launchd's
    # default ExitTimeOut (typically 20 s) would SIGKILL the worker part-way
    # and strand the jobs it had not reached yet.
    from sdlc.scheduler import _STOP_GRACE_SECONDS, DEFAULT_SLOTS

    argv = plist["ProgramArguments"]
    slots = int(argv[argv.index("--slots") + 1]) if "--slots" in argv else DEFAULT_SLOTS
    assert plist["ExitTimeOut"] > slots * 2 * _STOP_GRACE_SECONDS


def test_jobs_run_at_interactive_priority(plist) -> None:
    # Jobs are the worker's children and inherit its launchd resource class:
    # `Background` throttles CPU and disk I/O (efficiency cores only on Apple
    # Silicon), which would stretch a build past its queue wall-clock budget.
    assert plist["ProcessType"] == "Interactive"


def test_logs_live_under_the_sdlc_state_dir(plist) -> None:
    # One file for both streams, so the worker's narration (stdout) and its
    # errors (stderr) read in order.
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert plist["StandardOutPath"] == "__HOME__/.local/state/sdlc/worker.log"


def test_worker_state_resolves_where_an_xdg_shell_looks(plist, monkeypatch) -> None:
    # launchd hands the worker a bare environment: unpinned, it would drain
    # ~/.sdlc/queue.db and record its runs in ~/.sdlc/registry.json while the
    # shell's `sdlc queue`, `sdlc doctor` and dashboard read ~/.local/state/sdlc/.
    rendered = _rendered(plist)
    env = rendered["EnvironmentVariables"]
    monkeypatch.delenv("SDLC_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("SDLC_QUEUE_PATH", env["SDLC_QUEUE_PATH"])
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    state = Path(HOME) / ".local/state/sdlc"

    assert default_queue_path() == state / "queue.db"
    assert default_registry_path() == state / "registry.json"
    assert Path(rendered["StandardOutPath"]).parent == state


def test_a_shell_following_the_header_enqueues_where_the_worker_drains(plist, monkeypatch) -> None:
    # macOS sets no XDG_STATE_HOME: unless the shell exports what the header says,
    # `sdlc queue add` and `--enqueue` write ~/.sdlc/queue.db, which the worker
    # never drains, and `sdlc doctor` warns about the split.
    header, _, _ = TEMPLATE.read_text(encoding="utf-8").partition("-->")
    exported = re.search(r'export XDG_STATE_HOME="\$HOME/([^"]+)"', header)
    assert exported is not None
    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", f"{HOME}/{exported.group(1)}")

    assert default_queue_path() == Path(_rendered(plist)["EnvironmentVariables"]["SDLC_QUEUE_PATH"])


def test_path_reaches_the_tools_a_job_runs(plist) -> None:
    # launchd's own PATH is only /usr/bin:/bin:/usr/sbin:/sbin. Jobs need sdlc and
    # claude (~/.local/bin), nix-darwin and Homebrew tools, and /usr/bin's caffeinate.
    path = plist["EnvironmentVariables"]["PATH"].split(":")
    for entry in (
        "__HOME__/.local/bin",
        "/etc/profiles/per-user/__USER__/bin",
        "/run/current-system/sw/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
    ):
        assert entry in path


def test_no_secret_is_baked_in(plist) -> None:
    # A worker uses its own Claude/Codex/gh/glab logins; the plist carries no token.
    assert not [k for k in plist["EnvironmentVariables"] if "TOKEN" in k.upper()]


def test_worker_argv_names_the_m3max_and_both_pools(plist) -> None:
    argv = plist["ProgramArguments"]
    assert argv[0] == "__HOME__/.local/bin/sdlc"
    assert argv[1:] == [
        "queue", "run",
        "--worker", "m3max", "--pool", "claude-m3", "--pool", "codex-shared",
        "--dashboard-url", "http://__TAILNET_IP__:8787", "--follow",
    ]


def test_template_argv_is_accepted_by_queue_run(plist, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen: dict = {}

    def fake_detect(name, *, pools=(), host=None, **_):
        seen["name"], seen["pools"] = name, list(pools)
        return WorkerProfile(name=name, host="m3", pools=list(pools))

    def fake_run_queue(store, **kwargs):
        seen["config"] = kwargs["config"]
        return SchedulerResult()

    monkeypatch.setattr("sdlc.queue_worker.detect_worker_profile", fake_detect)
    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)

    result = runner.invoke(app, _rendered(plist)["ProgramArguments"][1:])

    assert result.exit_code == 0, result.output
    assert seen["name"] == "m3max"
    assert seen["pools"] == ["claude-m3", "codex-shared"]
    # Heartbeating while idle is what keeps `sdlc doctor` seeing it online.
    assert seen["config"].follow is True


# --- the sibling dashboard agent (Story 35.4-006) -------------------------------------


@pytest.fixture(scope="module")
def dashboard_plist() -> dict:
    with DASHBOARD_TEMPLATE.open("rb") as fh:
        return plistlib.load(fh)


def test_dashboard_template_is_a_valid_plist(dashboard_plist) -> None:
    assert dashboard_plist["Label"] == "com.fxmartin.sdlc-dashboard"
    assert DASHBOARD_TEMPLATE.name == f"{dashboard_plist['Label']}.plist"
    assert dashboard_plist["RunAtLoad"] is True and dashboard_plist["KeepAlive"] is True


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_dashboard_template_passes_plutil_lint() -> None:
    subprocess.run(["plutil", "-lint", str(DASHBOARD_TEMPLATE)], check=True, capture_output=True)


@pytest.mark.parametrize("template", [TEMPLATE, DASHBOARD_TEMPLATE])
def test_both_install_seds_fill_every_placeholder(template: Path) -> None:
    header, _, body = template.read_text(encoding="utf-8").partition("-->")
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", header))
    assert "__TAILNET_IP__" in substituted
    assert set(re.findall(r"__[A-Z_]+__", body)) <= substituted


def test_the_worker_advertises_the_address_its_dashboard_binds(plist, dashboard_plist) -> None:
    # The relay only has something to relay when these two agree.
    advertised = _rendered(plist)["ProgramArguments"]
    url = advertised[advertised.index("--dashboard-url") + 1]
    argv = dashboard_plist["ProgramArguments"].copy()
    host = argv[argv.index("--host") + 1].replace("__TAILNET_IP__", TAILNET_IP)
    port = argv[argv.index("--port") + 1]
    assert url == f"http://{host}:{port}"


def test_the_dashboard_binds_the_tailnet_never_every_interface(dashboard_plist) -> None:
    argv = dashboard_plist["ProgramArguments"]
    assert argv[argv.index("--host") + 1] == "__TAILNET_IP__"
    assert "0.0.0.0" not in argv


def test_the_dashboard_reads_the_registry_the_worker_writes(plist, dashboard_plist) -> None:
    assert (
        dashboard_plist["EnvironmentVariables"]["XDG_STATE_HOME"]
        == plist["EnvironmentVariables"]["XDG_STATE_HOME"]
    )


def test_dashboard_template_argv_is_accepted_by_sdlc_dashboard(
    dashboard_plist, tmp_path, monkeypatch
) -> None:
    from sdlc.build import IN_TEST_ENV_VAR

    seen: dict = {}
    monkeypatch.delenv(IN_TEST_ENV_VAR, raising=False)  # the recursion guard would skip serve
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sdlc.dashboard.serve", lambda *a, **kw: seen.update(args=a, kwargs=kw)
    )
    text = plistlib.dumps(dashboard_plist).decode().replace("__TAILNET_IP__", TAILNET_IP)
    argv = plistlib.loads(text.encode())["ProgramArguments"][1:]

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert seen["kwargs"]["host"] == TAILNET_IP and seen["kwargs"]["port"] == 8787


@pytest.mark.parametrize("tailnet_ip", ["", "  "])
def test_a_dashboard_rendered_without_a_tailnet_ip_never_binds_every_interface(
    dashboard_plist, tailnet_ip, monkeypatch
) -> None:
    # `tailscale ip -4` prints nothing while Tailscale is stopped or logged out, and
    # Python binds a blank host as every interface: the login-less dashboard would
    # serve /api/status, /api/logs and /log on whatever network the Mac is on.
    from sdlc.build import IN_TEST_ENV_VAR

    served: list[dict] = []
    monkeypatch.delenv(IN_TEST_ENV_VAR, raising=False)  # the refusal, not the recursion guard
    monkeypatch.setattr("sdlc.dashboard.serve", lambda *a, **kw: served.append(kw))
    text = plistlib.dumps(dashboard_plist).decode().replace("__TAILNET_IP__", tailnet_ip)
    argv = plistlib.loads(text.encode())["ProgramArguments"][1:]

    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert served == []
    assert "every interface" in result.stderr


def _install_recipe(template: Path) -> str:
    """The header's `Install:` commands, exactly as an operator pastes them."""
    header = template.read_text(encoding="utf-8").partition("-->")[0]
    recipe = re.search(r"Install:\n\n(.*?)\n\n", header, re.S)
    assert recipe is not None
    return textwrap.dedent(recipe.group(1))


def _install(template: Path, tmp_path: Path, tailnet_ip: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Run ``template``'s install recipe under a fake home, `tailscale` and `launchctl`."""
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    (home / "Library/LaunchAgents").mkdir(parents=True)
    bin_dir.mkdir()
    printed = f"echo {tailnet_ip}" if tailnet_ip else "true"  # logged out: prints nothing
    for name, body in (("tailscale", printed), ("launchctl", f'echo "$@" >> "{bin_dir}/launchctl.calls"')):
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        tool.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", _install_recipe(template)],
        cwd=REPO_ROOT,
        env={"HOME": str(home), "USER": "fx", "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, home / "Library/LaunchAgents" / template.name, bin_dir / "launchctl.calls"


@pytest.mark.parametrize("template", [TEMPLATE, DASHBOARD_TEMPLATE])
def test_the_install_stops_before_writing_an_agent_without_a_tailnet_address(
    template: Path, tmp_path: Path
) -> None:
    # Substituting "" would bind the dashboard to every interface and advertise
    # `http://:8787`, which the worker rejects at every start.
    result, installed, launchctl_calls = _install(template, tmp_path, tailnet_ip="")

    assert result.returncode != 0
    assert "tailscale ip -4 printed no address" in result.stderr
    assert not installed.exists()
    assert not launchctl_calls.exists()


@pytest.mark.parametrize("template", [TEMPLATE, DASHBOARD_TEMPLATE])
def test_the_install_renders_the_tailnet_address_and_loads_the_agent(
    template: Path, tmp_path: Path
) -> None:
    result, installed, launchctl_calls = _install(template, tmp_path, tailnet_ip=TAILNET_IP)

    assert result.returncode == 0, result.stderr
    agent = plistlib.loads(installed.read_bytes())
    assert not re.search(r"__[A-Z_]+__", plistlib.dumps(agent).decode())  # comments aside
    argv = agent["ProgramArguments"]
    assert TAILNET_IP in argv or f"http://{TAILNET_IP}:8787" in argv
    assert launchctl_calls.read_text(encoding="utf-8").split() == [
        "bootstrap", f"gui/{os.getuid()}", str(installed),
    ]
