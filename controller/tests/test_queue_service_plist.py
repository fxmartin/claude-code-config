# ABOUTME: Tests for the launchd plist template that keeps `sdlc queue serve` resident.
# ABOUTME: Story 35.1-003. Structure via plistlib everywhere; `plutil -lint` where macOS has it.

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "templates"
    / "launchd"
    / "com.fxmartin.sdlc-queue.plist"
)


@pytest.fixture(scope="module")
def plist() -> dict:
    with TEMPLATE.open("rb") as fh:
        return plistlib.load(fh)


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_template_passes_plutil_lint() -> None:
    proc = subprocess.run(
        ["plutil", "-lint", str(TEMPLATE)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_label_matches_the_file_name(plist: dict) -> None:
    assert plist["Label"] == "com.fxmartin.sdlc-queue"
    assert TEMPLATE.name == f"{plist['Label']}.plist"


def test_runs_queue_serve_with_a_bind(plist: dict) -> None:
    args = plist["ProgramArguments"]
    assert args[1:3] == ["queue", "serve"]
    assert args[args.index("--bind") + 1].endswith(":8790")


def test_starts_at_boot_and_is_kept_alive(plist: dict) -> None:
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True


def test_logs_under_the_sdlc_state_dir(plist: dict) -> None:
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].endswith("/.local/state/sdlc/" + plist[key].rsplit("/", 1)[1])


def test_store_is_pinned_next_to_the_logs(plist: dict) -> None:
    # launchd hands the service a bare environment, so without this the service
    # would resolve ~/.sdlc/queue.db while an operator's XDG shell resolves
    # ~/.local/state/sdlc/queue.db — two stores, one of them unserved.
    assert plist["EnvironmentVariables"]["SDLC_QUEUE_PATH"].endswith(
        "/.local/state/sdlc/queue.db"
    )


def test_no_secret_is_baked_in(plist: dict) -> None:
    # The bearer token is delivered out of band (env/file), never in the plist.
    assert not [k for k in plist["EnvironmentVariables"] if "TOKEN" in k.upper()]


def test_never_binds_a_wildcard(plist: dict) -> None:
    args = plist["ProgramArguments"]
    bind = args[args.index("--bind") + 1]
    assert not bind.startswith(("0.0.0.0", "::", "[::"))
