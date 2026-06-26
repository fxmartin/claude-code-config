# ABOUTME: Tests for the Codex build/QA adapter (Story 20.3-001) — the registry
# ABOUTME: round-trip, the codex-exec parser selection, and the zero-claude property.

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

# The checked-in registry — the one a real run loads. Exercising the adapter
# against it (rather than a bespoke tmp file) is what proves AC1: "harnesses.yaml
# has a codex entry ... a build dispatches a build/coverage agent to it".
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"

_VALID_BUILD = {
    "branch_name": "feature/20.3-001",
    "build_status": "SUCCESS",
    "commit_sha": "deadbeef",
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
    """A Codex transcript ending in the harness-neutral result block."""
    body = json.dumps(payload)
    return f"codex prose and reasoning\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_codex_entry_uses_the_codex_exec_parser() -> None:
    """The registry's codex entry declares the no-telemetry codex-exec parser."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    assert codex.parser == "codex-exec"
    assert isinstance(get_parser(codex.parser), PlainResultParser)


def test_codex_argv_never_invokes_claude() -> None:
    """The codex harness renders its own command — no `claude` in the argv (AC3)."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    argv = codex.to_argv()
    assert argv, "codex harness rendered an empty command"
    assert not any("claude" in token for token in argv)


def test_build_agent_round_trips_through_codex(monkeypatch) -> None:
    """A build agent dispatched to codex returns the contract and validates (AC1/AC2)."""
    seen_cmd: list[str] = []
    seen_input: list[str | None] = []

    def fake_run(cmd, **kwargs):
        seen_cmd[:] = cmd
        seen_input.append(kwargs.get("input"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    result = dispatch_on_harness(codex, "build", "build story 20.3-001")

    assert result.data["build_status"] == "SUCCESS"
    assert result.data["commit_sha"] == "deadbeef"
    # The codex-exec parser records usage as unavailable, never fabricated (AC2/AC3).
    assert result.usage_available is False
    assert result.usage is None
    assert result.cost_usd is None
    # The agent ran on codex, not claude, and got the prompt on stdin.
    assert not any("claude" in token for token in seen_cmd)
    assert seen_input == ["build story 20.3-001"]


def test_coverage_agent_round_trips_through_codex(monkeypatch) -> None:
    """A coverage/QA agent dispatched to codex validates against its schema (AC1)."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_COVERAGE))
    )

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    result = dispatch_on_harness(codex, "coverage", "qa story 20.3-001")

    assert result.data["coverage_status"] == "PASS"
    assert result.usage_available is False


def test_codex_nonzero_exit_is_plain_dispatch_error(monkeypatch) -> None:
    """A codex failure is a plain dispatch error — the parser never fabricates a 429."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="codex blew up"),
    )

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    with pytest.raises(AgentDispatchError) as excinfo:
        dispatch_on_harness(codex, "build", "prompt")
    # A plain AgentDispatchError, not a RateLimitError subclass.
    assert type(excinfo.value) is AgentDispatchError


def test_full_codex_run_spawns_zero_claude() -> None:
    """Every pipeline role routed to codex resolves to a claude-free argv (AC3)."""
    role_map = {role: "codex" for role in PIPELINE_ROLES}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)

    assert set(resolved) == set(PIPELINE_ROLES)
    for role, harness in resolved.items():
        assert harness.name == "codex", f"role {role} did not route to codex"
        assert not any("claude" in token for token in harness.to_argv())


def test_dispatch_on_harness_passes_through_dispatch_kwargs(monkeypatch) -> None:
    """Extra dispatch kwargs (e.g. cwd) reach dispatch_agent unchanged."""
    seen_cwd: list[object] = []

    def fake_run(cmd, **kwargs):
        seen_cwd.append(kwargs.get("cwd"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    worktree = Path("/tmp/story-worktree")
    dispatch_on_harness(codex, "build", "prompt", cwd=worktree)

    assert seen_cwd == [worktree]
