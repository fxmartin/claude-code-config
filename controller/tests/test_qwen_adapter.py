# ABOUTME: Tests for the Qwen Code harness adapter registry integration.
# ABOUTME: Proves Qwen dispatch uses the plain result parser and never invokes Claude.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.dispatch import AgentDispatchError
from sdlc.harness import dispatch_on_harness, resolve_harness
from sdlc.parsers import PlainResultParser, get_parser
from sdlc.role_routing import PIPELINE_ROLES, resolve_role_routing

CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"

_VALID_BUILD = {
    "branch_name": "feature/qwen",
    "build_status": "SUCCESS",
    "commit_sha": "feedface",
}


def _wrap(payload: dict) -> str:
    body = json.dumps(payload)
    return f"qwen prose and reasoning\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_qwen_entry_uses_plain_result_parser() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    assert qwen.parser == "codex-exec"
    assert isinstance(get_parser(qwen.parser), PlainResultParser)


def test_qwen_entry_declares_probe_and_safe_capabilities() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    assert qwen.probe == "qwen --version"
    assert qwen.capabilities["json_contract"] is True
    assert qwen.capabilities["usage_tracking"] is False
    assert qwen.capabilities["rate_limit_aware"] is False


def test_qwen_argv_never_invokes_claude() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    argv = qwen.to_argv()
    assert argv == ["qwen-build-adapter.sh"]
    assert not any("claude" in token for token in argv)


def test_build_agent_round_trips_through_qwen(monkeypatch) -> None:
    seen_cmd: list[str] = []
    seen_input: list[str | None] = []

    def fake_run(cmd, **kwargs):
        seen_cmd[:] = cmd
        seen_input.append(kwargs.get("input"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    result = dispatch_on_harness(qwen, "build", "build story with qwen")

    assert result.data["build_status"] == "SUCCESS"
    assert result.data["commit_sha"] == "feedface"
    assert result.usage_available is False
    assert result.usage is None
    assert result.cost_usd is None
    assert not any("claude" in token for token in seen_cmd)
    assert seen_input == ["build story with qwen"]


def test_qwen_nonzero_exit_is_plain_dispatch_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="qwen blew up"),
    )

    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    with pytest.raises(AgentDispatchError) as excinfo:
        dispatch_on_harness(qwen, "build", "prompt")
    assert type(excinfo.value) is AgentDispatchError


def test_full_qwen_run_spawns_zero_claude() -> None:
    role_map = {role: "qwen" for role in PIPELINE_ROLES}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)

    assert set(resolved) == set(PIPELINE_ROLES)
    for role, harness in resolved.items():
        assert harness.name == "qwen", f"role {role} did not route to qwen"
        assert not any("claude" in token for token in harness.to_argv())
