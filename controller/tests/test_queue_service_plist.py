# ABOUTME: Tests for the launchd plist template that keeps `sdlc queue serve` resident.
# ABOUTME: Story 35.1-003. Structure via plistlib everywhere; `plutil -lint` where macOS has it.

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sdlc.doctor import QUEUE_SERVICE_LABEL, default_queue_service_plist
from sdlc.queue_server import parse_bind

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "templates" / "launchd" / "com.fxmartin.sdlc-queue.plist"
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "controller-architecture.md"


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


def test_doctor_looks_for_the_templates_label(plist: dict) -> None:
    # `sdlc doctor` finds the agent by file name, and its remedy and the
    # recovery runbook address it by label: all of them must be this template's.
    assert plist["Label"] == QUEUE_SERVICE_LABEL
    assert default_queue_service_plist().name == TEMPLATE.name


def test_the_documented_nix_snippet_keeps_the_label() -> None:
    # nix-darwin names the plist after serviceConfig.Label, and its default
    # (org.nixos.sdlc-queue) would read to doctor as "not installed".
    doc = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    assert f'Label = "{QUEUE_SERVICE_LABEL}";' in doc


def test_the_install_sed_fills_every_placeholder() -> None:
    # The header's `sed` must cover every placeholder in the plist body, or the
    # rendered agent would exec a literal `__HOME__/.local/bin/sdlc`.
    header, _, body = TEMPLATE.read_text(encoding="utf-8").partition("-->")
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", header))
    assert substituted
    assert set(re.findall(r"__[A-Z_]+__", body)) <= substituted


def test_runs_queue_serve_with_a_bind(plist: dict) -> None:
    args = plist["ProgramArguments"]
    assert args[1:3] == ["queue", "serve"]
    assert args[args.index("--bind") + 1].endswith(":8790")


def test_starts_when_loaded_and_is_kept_alive(plist: dict) -> None:
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


def test_rendered_bind_passes_the_servers_own_bind_rules(plist: dict) -> None:
    # Held to `sdlc queue serve`'s rules (a tailnet or loopback IP, never a
    # wildcard) rather than to a text prefix: render it as the install does.
    args = plist["ProgramArguments"]
    bind = args[args.index("--bind") + 1]
    assert bind.startswith("__TAILNET_IP__:")
    rendered = bind.replace("__TAILNET_IP__", "100.101.102.103")
    assert parse_bind(rendered) == ("100.101.102.103", 8790)
