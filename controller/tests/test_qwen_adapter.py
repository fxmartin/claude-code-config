# ABOUTME: Tests for the Qwen Code harness adapter registry integration.
# ABOUTME: Proves Qwen dispatch uses the plain result parser and never invokes Claude.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdlc.capability import MODE_PARALLEL, preflight_harness, resolve_capabilities
from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.degradation import DegradationKind, evaluate_degradations
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


# ---------------------------------------------------------------------------
# Story 29.1-001: worktree_isolation/parallel verified-negative on this host
# (evidence: controller/eval/results/qwen-worktree-smoke-29.1-001.json) — qwen
# never got past its own auth check inside a fresh worktree, so the flags stay
# false rather than being flipped without proof. These tests lock the current
# degradation behaviour as a regression guard: they must be updated (with new
# evidence) if the harnesses.yaml capabilities are ever flipped to true.
# ---------------------------------------------------------------------------


def test_qwen_parallel_request_still_degrades_to_serial() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    capabilities = resolve_capabilities(qwen)
    plan = evaluate_degradations(qwen.name, capabilities, requested_mode=MODE_PARALLEL)

    assert plan.has(DegradationKind.PARALLEL_TO_SERIAL)
    assert plan.effective_mode != MODE_PARALLEL


def test_qwen_preflight_warns_on_parallel_request() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    pf = preflight_harness(qwen, requested_mode=MODE_PARALLEL)

    assert pf.degraded is True
    assert any("parallel" in warning for warning in pf.warnings)


def test_qwen_capabilities_declare_no_worktree_isolation_pending_evidence() -> None:
    qwen = resolve_harness("qwen", config_path=CONFIG_PATH)
    assert qwen.capabilities["worktree_isolation"] is False
    assert qwen.capabilities["parallel"] is False


def test_full_qwen_run_spawns_zero_claude() -> None:
    role_map = {role: "qwen" for role in PIPELINE_ROLES}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)

    assert set(resolved) == set(PIPELINE_ROLES)
    for role, harness in resolved.items():
        assert harness.name == "qwen", f"role {role} did not route to qwen"
        assert not any("claude" in token for token in harness.to_argv())
