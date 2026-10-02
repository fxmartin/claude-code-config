# ABOUTME: Tests for the resident-worker LaunchAgent template (Story 35.2-004): the plist
# ABOUTME: validates, and the argv it launches is what `sdlc queue run` actually accepts.

from __future__ import annotations

import plistlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.queue_worker import WorkerProfile
from sdlc.scheduler import SchedulerResult

runner = CliRunner()

TEMPLATE = Path(__file__).resolve().parents[2] / "templates/launchd/com.fxmartin.sdlc-worker.plist"


@pytest.fixture(scope="module")
def plist() -> dict:
    with TEMPLATE.open("rb") as fh:
        return plistlib.load(fh)


def _script(plist: dict) -> str:
    argv = plist["ProgramArguments"]
    assert argv[:2] == ["/bin/sh", "-c"]
    return argv[2]


def _worker_argv(plist: dict) -> list[str]:
    """The `sdlc …` command the shell wrapper execs, minus its log redirect."""
    match = re.search(r"exec (sdlc .*?) >>", _script(plist))
    assert match, "the wrapper must exec sdlc with its output redirected to a log"
    return shlex.split(match.group(1))


def test_template_is_a_valid_plist(plist) -> None:
    assert plist["Label"] == "com.fxmartin.sdlc-worker"
    assert TEMPLATE.name == f"{plist['Label']}.plist"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_template_passes_plutil_lint() -> None:
    subprocess.run(["plutil", "-lint", str(TEMPLATE)], check=True, capture_output=True)


def test_worker_starts_at_boot_and_is_restarted_on_exit(plist) -> None:
    # KeepAlive is what turns the stale-controller refusal into a restart.
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 10  # a crash loop must not spin


def test_logs_live_under_the_sdlc_state_dir(plist) -> None:
    script = _script(plist)
    assert '$HOME/.local/state/sdlc/' in script
    assert 'mkdir -p "$HOME/.local/state/sdlc"' in script
    assert "2>&1" in script  # stderr is where the controller reports


def test_worker_argv_names_the_m3max_and_both_pools(plist) -> None:
    argv = _worker_argv(plist)
    assert argv[:3] == ["sdlc", "queue", "run"]
    assert argv[3:] == [
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

    result = runner.invoke(app, _worker_argv(plist)[1:])

    assert result.exit_code == 0, result.output
    assert seen["name"] == "m3max"
    assert seen["pools"] == ["claude-m3", "codex-shared"]
    # Heartbeating while idle is what keeps `sdlc doctor` seeing it online.
    assert seen["config"].follow is True
