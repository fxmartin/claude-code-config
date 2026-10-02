# ABOUTME: Tests for the resident-worker LaunchAgent template (Story 35.2-004): the plist
# ABOUTME: validates, and the argv it launches is what `sdlc queue run` actually accepts.

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
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

TEMPLATE = Path(__file__).resolve().parents[2] / "templates/launchd/com.fxmartin.sdlc-worker.plist"
HOME = "/Users/fx"


@pytest.fixture(scope="module")
def plist() -> dict:
    with TEMPLATE.open("rb") as fh:
        return plistlib.load(fh)


def _rendered(plist: dict) -> dict:
    """The plist as the header's install `sed` writes it for user ``fx``."""
    text = plistlib.dumps(plist).decode()
    return plistlib.loads(text.replace("__HOME__", HOME).replace("__USER__", "fx").encode())


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


def test_worker_starts_at_boot_and_is_restarted_on_exit(plist) -> None:
    # KeepAlive restarts the worker after it exits 75 on a controller upgrade.
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 10  # a crash loop must not spin


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
        "--worker", "m3max", "--pool", "claude-m3", "--pool", "codex-shared", "--follow",
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
