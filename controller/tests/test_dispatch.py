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
from sdlc.dispatch import (
    DEFAULT_AGENT_CMD,
    AgentDispatchError,
    AgentResult,
    dispatch_agent,
    resolve_agent_cmd,
)


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


# --- R7: headless / configurable agent command ----------------------------


def test_default_agent_cmd_is_headless() -> None:
    """The default command bypasses permissions so a -p agent can write/commit."""
    assert "--dangerously-skip-permissions" in DEFAULT_AGENT_CMD


def test_resolve_agent_cmd_explicit_wins(monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "should-be-ignored")
    assert resolve_agent_cmd(["my", "agent"]) == ["my", "agent"]


def test_resolve_agent_cmd_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --permission-mode acceptEdits")
    assert resolve_agent_cmd() == ["claude", "-p", "--permission-mode", "acceptEdits"]


def test_resolve_agent_cmd_default(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    assert resolve_agent_cmd() == DEFAULT_AGENT_CMD


# --- R8: transcript persistence --------------------------------------------


def test_dispatch_writes_transcript_on_success(monkeypatch, tmp_path) -> None:
    out = _wrap(_VALID_BUILD)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    tpath = tmp_path / "build-1.log"
    dispatch_agent("build", "prompt", agent_cmd=["fake"], transcript_path=tpath)
    assert tpath.read_text(encoding="utf-8") == out


def test_dispatch_writes_transcript_on_contract_failure(monkeypatch, tmp_path) -> None:
    """Even when the result block is missing, the transcript is persisted (R8)."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted("garbage, no markers")
    )
    tpath = tmp_path / "build-1.log"
    with pytest.raises(ResultBlockError):
        dispatch_agent("build", "prompt", agent_cmd=["fake"], transcript_path=tpath)
    assert "garbage, no markers" in tpath.read_text(encoding="utf-8")


# --- Token/cost: --output-format json envelope ----------------------------


def _envelope(result_text: str, *, is_error: bool = False, cost: float = 0.42,
              usage: dict | None = None, session_id: str = "sess-123") -> str:
    """A claude -p --output-format json result envelope, as a JSON string."""
    return json.dumps({
        "type": "result",
        "subtype": "error_max_turns" if is_error else "success",
        "is_error": is_error,
        "result": result_text,
        "session_id": session_id,
        "total_cost_usd": cost,
        "num_turns": 3,
        "duration_ms": 1234,
        "usage": usage if usage is not None else {
            "input_tokens": 100, "output_tokens": 20,
            "cache_creation_input_tokens": 300, "cache_read_input_tokens": 4000,
        },
    })


def test_dispatch_unwraps_json_envelope_and_captures_usage(monkeypatch) -> None:
    """An --output-format json envelope is unwrapped; usage/cost/session captured."""
    out = _envelope(_wrap(_VALID_BUILD))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    result = dispatch_agent("build", "prompt", agent_cmd=["fake"])
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.session_id == "sess-123"
    assert result.cost_usd == 0.42
    assert result.usage["output_tokens"] == 20
    assert result.usage["cache_read_input_tokens"] == 4000
    # raw is the readable agent text, not the JSON wrapper.
    assert RESULT_END_MARKER in result.raw


def test_dispatch_envelope_with_fenced_result_recovers(monkeypatch) -> None:
    """R10 tolerant parsing still applies to the envelope's `result` text."""
    fenced = "I built and committed it.\n```json\n" + json.dumps(_VALID_BUILD) + "\n```\n"
    out = _envelope(fenced)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    result = dispatch_agent("build", "prompt", agent_cmd=["fake"])
    assert result.data["build_status"] == "SUCCESS"
    assert result.cost_usd == 0.42


def test_dispatch_envelope_is_error_raises(monkeypatch) -> None:
    """An envelope with is_error=true surfaces as AgentDispatchError."""
    out = _envelope("hit the turn limit", is_error=True)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    with pytest.raises(AgentDispatchError, match="reported an error"):
        dispatch_agent("build", "prompt", agent_cmd=["fake"])


def test_dispatch_envelope_writes_readable_transcript(monkeypatch, tmp_path) -> None:
    """The persisted transcript is the agent text (+stderr), not the JSON envelope."""
    out = _envelope(_wrap(_VALID_BUILD))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(out, stderr="a warning")
    )
    tpath = tmp_path / "build-1.log"
    dispatch_agent("build", "prompt", agent_cmd=["fake"], transcript_path=tpath)
    body = tpath.read_text(encoding="utf-8")
    assert RESULT_START_MARKER in body
    assert '"type": "result"' not in body  # the envelope wrapper is gone
    assert "a warning" in body              # stderr is appended


def test_dispatch_malformed_json_envelope_falls_back(monkeypatch) -> None:
    """Output starting with '{' but not valid JSON falls back to raw parsing."""
    out = "{oops not json\n" + _wrap(_VALID_BUILD)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    result = dispatch_agent("build", "prompt", agent_cmd=["fake"])
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.usage is None  # not recognized as an envelope


def test_dispatch_non_result_json_is_not_treated_as_envelope(monkeypatch) -> None:
    """A valid JSON object that isn't a result envelope is not unwrapped."""
    out = json.dumps({"type": "system", "subtype": "init"})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(out))
    # Falls through to raw parsing → the dict fails schema validation (no build fields).
    with pytest.raises(SchemaValidationError):
        dispatch_agent("build", "prompt", agent_cmd=["fake"])


def test_dispatch_plain_text_fallback_has_no_usage(monkeypatch) -> None:
    """Non-envelope (plain text) output still parses, with usage None (back-compat)."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_BUILD))
    )
    result = dispatch_agent("build", "prompt", agent_cmd=["fake"])
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.usage is None and result.cost_usd is None and result.session_id is None


def test_default_agent_cmd_requests_json_output() -> None:
    """The default command asks for the JSON envelope so usage is captured."""
    assert "--output-format" in DEFAULT_AGENT_CMD
    i = DEFAULT_AGENT_CMD.index("--output-format")
    assert DEFAULT_AGENT_CMD[i + 1] == "json"
