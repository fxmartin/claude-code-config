# ABOUTME: Tests for the agent-dispatch boundary (Story 7.3-001).
# ABOUTME: subprocess is mocked — no real Claude Code agent is ever invoked.

from __future__ import annotations

import json
import subprocess

import pytest

from sdlc.contracts import (
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    SchemaValidationError,
    ResultBlockError,
)
from sdlc.dispatch import AgentDispatchError, AgentResult, dispatch_agent


def _wrap(payload: dict) -> str:
    body = json.dumps(payload)
    return f"agent prose\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


_VALID_BUILD = {
    "branch_name": "feature/7.3-001",
    "build_status": "SUCCESS",
    "commit_sha": "abc123",
}


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_dispatch_validates_and_returns_result(monkeypatch) -> None:
    """A well-formed agent response is parsed, validated, and returned."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dispatch_agent("build", "build story 7.3-001", agent_cmd=["fake-claude"])
    assert isinstance(result, AgentResult)
    assert result.agent_type == "build"
    assert result.data["branch_name"] == "feature/7.3-001"
    # The prompt is passed to the subprocess so the agent receives instructions.
    assert calls, "subprocess.run was never called"


def test_dispatch_passes_prompt_to_subprocess(monkeypatch) -> None:
    """The rendered prompt reaches the subprocess (argv or stdin)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "PROMPT-MARKER-XYZ", agent_cmd=["fake-claude"])
    combined = " ".join(seen["cmd"]) + (seen.get("input") or "")
    assert "PROMPT-MARKER-XYZ" in combined


def test_dispatch_raises_on_schema_validation_failure(monkeypatch) -> None:
    """A response missing a required field raises (routes to bugfix upstream)."""
    bad = {"build_status": "SUCCESS", "commit_sha": "abc"}  # no branch_name

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(bad))
    )
    with pytest.raises(SchemaValidationError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_raises_on_missing_result_block(monkeypatch) -> None:
    """A response with no marker block raises a ResultBlockError."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted("no markers here")
    )
    with pytest.raises(ResultBlockError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_raises_on_nonzero_exit(monkeypatch) -> None:
    """A non-zero subprocess exit raises AgentDispatchError before parsing."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="boom"),
    )
    with pytest.raises(AgentDispatchError, match="boom"):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_raises_on_timeout(monkeypatch) -> None:
    """A subprocess timeout surfaces as AgentDispatchError, not a raw exception."""

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AgentDispatchError, match="timed out"):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], timeout=1)


def test_dispatch_uses_default_agent_cmd(monkeypatch) -> None:
    """When no agent_cmd is given a sensible default (claude CLI) is used."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt")
    assert seen["cmd"][0]  # a non-empty executable name
