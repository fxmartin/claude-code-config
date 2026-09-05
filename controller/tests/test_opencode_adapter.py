# ABOUTME: Tests for the OpenCode harness adapter registry integration (Story 29.2-001).
# ABOUTME: Proves OpenCode dispatch uses the plain result parser and never invokes Claude.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.dispatch import AgentDispatchError
from sdlc.harness import dispatch_on_harness, load_harnesses_config, resolve_harness
from sdlc.parsers import PlainResultParser, get_parser
from sdlc.role_routing import PIPELINE_ROLES, resolve_role_routing

CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"

_VALID_BUILD = {
    "branch_name": "feature/opencode",
    "build_status": "SUCCESS",
    "commit_sha": "feedface",
}

_VALID_COVERAGE = {
    "pr_number": 42,
    "pr_url": "https://github.com/fxmartin/repo/pull/42",
    "coverage_pct": 91.5,
    "tests_added": 7,
    "coverage_status": "PASS",
    "security_status": "PASS",
}


def _wrap(payload: dict) -> str:
    body = json.dumps(payload)
    return f"opencode prose and reasoning\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_opencode_entry_uses_plain_result_parser() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    assert opencode.parser == "codex-exec"
    assert isinstance(get_parser(opencode.parser), PlainResultParser)


def test_opencode_entry_declares_probe_and_conservative_capabilities() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    assert opencode.probe == "opencode --version"
    assert opencode.capabilities["json_contract"] is True
    assert opencode.capabilities["worktree_isolation"] is False
    assert opencode.capabilities["parallel"] is False
    assert opencode.capabilities["usage_tracking"] is False
    assert opencode.capabilities["rate_limit_aware"] is False


def test_opencode_argv_never_invokes_claude() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    argv = opencode.to_argv()
    assert argv == ["opencode-build-adapter.sh"]
    assert not any("claude" in token for token in argv)


def test_shipped_opencode_harness_pins_no_model_entitlement() -> None:
    """Issue #228 lesson (echoed in the story notes): never hardcode a model id
    in the shipped template — the default must run on whatever the user has
    configured, with per-stage routing staying an opt-in.

    The cost of that choice is real and is deliberately paid, not hidden: if
    OpenCode's own resolved default points at an unreachable provider, the run
    hangs silently for the full captured-path wall clock. It is documented as a
    precondition with an `OPENCODE_FLAGS` escape hatch (pinned by
    :func:`test_opencode_entry_documents_the_unreachable_default_hang`) rather
    than papered over by hardcoding a model nobody is guaranteed to be entitled
    to.
    """
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    for stage in (None, "build", "coverage", "review", "merge", "adversarial"):
        argv = opencode.to_argv() if stage is None else opencode.to_argv(stage=stage)
        assert "--model" not in argv, (
            f"shipped opencode argv forces a model for stage={stage}: {argv}"
        )


def test_opencode_entry_documents_the_unreachable_default_hang() -> None:
    """The entry pins no model, so the *documentation* of that trade-off is the
    only mitigation a user gets before burning a silent 3600s stage. Pin it: an
    edit that drops the warning or the escape hatch must fail here."""
    entry = CONFIG_PATH.read_text().split("  opencode:")[0].rsplit("# OpenCode adapter", 1)[-1]
    assert "OPENCODE_FLAGS" in entry, (
        "opencode entry no longer names the redeploy-safe model override"
    )
    assert "opencode run --pure" in entry, (
        "opencode entry no longer shows how to check the resolved default answers"
    )
    assert "3600" in entry, (
        "opencode entry no longer states the wall clock a silent hang costs"
    )


def test_build_agent_round_trips_through_opencode(monkeypatch) -> None:
    seen_cmd: list[str] = []
    seen_input: list[str | None] = []

    def fake_run(cmd, **kwargs):
        seen_cmd[:] = cmd
        seen_input.append(kwargs.get("input"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    result = dispatch_on_harness(opencode, "build", "build story with opencode")

    assert result.data["build_status"] == "SUCCESS"
    assert result.data["commit_sha"] == "feedface"
    assert result.usage_available is False
    assert result.usage is None
    assert result.cost_usd is None
    assert not any("claude" in token for token in seen_cmd)
    assert seen_input == ["build story with opencode"]


def test_coverage_agent_round_trips_through_opencode(monkeypatch) -> None:
    """Only the build role is proven live per the story's field finding — this
    proves the coverage role at least round-trips through the same wrapper and
    parser, so "any pipeline role" is not resting solely on the build self-test."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_COVERAGE))
    )

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    result = dispatch_on_harness(opencode, "coverage", "qa story with opencode")

    assert result.data["coverage_status"] == "PASS"
    assert result.usage_available is False


def test_opencode_nonzero_exit_is_plain_dispatch_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="opencode blew up"),
    )

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    with pytest.raises(AgentDispatchError) as excinfo:
        dispatch_on_harness(opencode, "build", "prompt")
    assert type(excinfo.value) is AgentDispatchError


def test_full_opencode_run_spawns_zero_claude() -> None:
    role_map = {role: "opencode" for role in PIPELINE_ROLES}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)

    assert set(resolved) == set(PIPELINE_ROLES)
    for role, harness in resolved.items():
        assert harness.name == "opencode", f"role {role} did not route to opencode"
        assert not any("claude" in token for token in harness.to_argv())


def test_registry_bare_adapter_commands_are_installed_on_path() -> None:
    """Regression guard for Story 29.2-001's dispatch gap.

    Registry entries invoke their wrapper by BARE NAME, resolved on PATH at
    dispatch, and `install.sh --core` is what puts those wrappers on PATH. A new
    harness whose adapter the installer does not symlink resolves to nothing on a
    PATH-installed controller and the stage dies with "command not found" — which
    is exactly what the shipped `opencode` entry did. Assert the invariant for
    every registry adapter, not just this story's.
    """
    core_sh = Path(__file__).resolve().parents[2] / "install" / "core.sh"
    if not core_sh.is_file():
        pytest.skip("install/core.sh is not present (installed controller, not a checkout)")
    installer = core_sh.read_text(encoding="utf-8")

    registry = load_harnesses_config(CONFIG_PATH)
    adapters = sorted(
        {
            token
            for entry in registry.values()
            for token in [entry.command.split()[0]]
            if token.endswith("-adapter.sh") and "/" not in token
        }
    )
    assert "opencode-build-adapter.sh" in adapters
    for adapter in adapters:
        assert f'create_symlink "$SCRIPT_DIR/scripts/{adapter}"' in installer, (
            f"{adapter} is dispatched by bare name but install/core.sh never "
            f"symlinks it onto PATH"
        )
        assert f'remove_symlink "$bin_dir/{adapter}"' in installer, (
            f"{adapter} is symlinked onto PATH but --uninstall never removes it"
        )
