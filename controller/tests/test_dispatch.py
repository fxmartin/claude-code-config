# ABOUTME: Tests for the agent-dispatch boundary (Story 7.3-001).
# ABOUTME: subprocess is mocked — no real Claude Code agent is ever invoked.

from __future__ import annotations

import json
import subprocess
import threading

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
    """When no agent_cmd is given a sensible default (claude CLI) is used.

    The default is a streaming command (11.1-001), so it launches via Popen;
    the streaming coverage lives in ``test_default_streaming_dispatch_uses_default_cmd``.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted(_wrap(_VALID_BUILD))

    # A non-streaming explicit command still exercises the captured run() path.
    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
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


def test_default_agent_cmd_requests_stream_json_output() -> None:
    """The default command streams (stream-json + --verbose) so a stage is live (11.1-001)."""
    assert "--output-format" in DEFAULT_AGENT_CMD
    i = DEFAULT_AGENT_CMD.index("--output-format")
    assert DEFAULT_AGENT_CMD[i + 1] == "stream-json"
    # `claude -p` requires --verbose to actually emit the line-delimited stream.
    assert "--verbose" in DEFAULT_AGENT_CMD


# --- 11.1-001: streaming dispatch with a live transcript tee ---------------


class _FakeStdin:
    def __init__(self) -> None:
        self.written = ""
        self.closed = False

    def write(self, data: str) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


class _FakeStderr:
    def __init__(self, data: str = "") -> None:
        self._data = data

    def read(self) -> str:
        return self._data


class _FakePopen:
    """A minimal stand-in for ``subprocess.Popen`` over a stream-json agent."""

    def __init__(
        self,
        stdout_lines,
        *,
        returncode: int = 0,
        stderr: str = "",
        wait_exc: Exception | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = iter(stdout_lines)
        self.stderr = _FakeStderr(stderr)
        self._returncode = returncode
        self._wait_exc = wait_exc
        self.killed = False
        self.wait_delay = 0.0  # seconds wait() lingers, to provoke timer races

    def wait(self, timeout=None):  # noqa: ANN001 - mirror subprocess API
        if self.wait_delay:
            # Block long enough for a short-timeout watchdog to fire mid-reap.
            threading.Event().wait(self.wait_delay)
        if self._wait_exc is not None:
            raise self._wait_exc
        return self._returncode

    def kill(self) -> None:
        self.killed = True

    def poll(self):
        return self._returncode


def _stream_result_event(result_text: str, *, is_error: bool = False,
                         cost: float = 0.42, usage: dict | None = None,
                         session_id: str = "sess-123") -> str:
    """A terminal stream-json `result` event line (same shape as the json envelope)."""
    return _envelope(result_text, is_error=is_error, cost=cost,
                     usage=usage, session_id=session_id) + "\n"


_STREAM_PREAMBLE = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "sess-123"}) + "\n",
    json.dumps({"type": "assistant", "message": {"content": "working"}}) + "\n",
    json.dumps({"type": "user", "message": {"content": "tool result"}}) + "\n",
]

_STREAM_CMD = ["claude", "-p", "--output-format", "stream-json", "--verbose"]


def test_streaming_dispatch_extracts_result_and_usage(monkeypatch) -> None:
    """A stream-json command is consumed line-by-line; the terminal result is used."""
    lines = list(_STREAM_PREAMBLE) + [_stream_result_event(_wrap(_VALID_BUILD))]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.session_id == "sess-123"
    assert result.cost_usd == 0.42
    assert result.usage["output_tokens"] == 20
    assert RESULT_END_MARKER in result.raw


def test_streaming_dispatch_passes_prompt_on_stdin(monkeypatch) -> None:
    """The prompt is written to the streamed subprocess's stdin and closed."""
    fake = _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    dispatch_agent("build", "PROMPT-MARKER-XYZ", agent_cmd=_STREAM_CMD)
    assert "PROMPT-MARKER-XYZ" in fake.stdin.written
    assert fake.stdin.closed


def test_streaming_dispatch_tees_stream_to_transcript(monkeypatch, tmp_path) -> None:
    """Every stream line lands verbatim in the transcript (the live tail -f view)."""
    lines = list(_STREAM_PREAMBLE) + [_stream_result_event(_wrap(_VALID_BUILD))]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))
    tpath = tmp_path / "build-1.log"
    dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, transcript_path=tpath)
    body = tpath.read_text(encoding="utf-8")
    # The raw stream is preserved (system/assistant/result lines), not rewritten.
    assert '"type": "system"' in body
    assert '"type": "result"' in body
    assert RESULT_START_MARKER in body


def test_streaming_dispatch_tee_is_incremental(monkeypatch, tmp_path) -> None:
    """Lines are flushed as they arrive: line N is on disk before line N+1 is read."""
    tpath = tmp_path / "build-1.log"
    first = json.dumps({"type": "system", "subtype": "init"}) + "\n"
    second = _stream_result_event(_wrap(_VALID_BUILD))

    def gen():
        yield first
        # By the time the second line is requested, the first must already be
        # flushed to disk — otherwise `tail -f` would lag a whole stage.
        assert first in tpath.read_text(encoding="utf-8")
        yield second

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(gen()))
    dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, transcript_path=tpath)


def test_streaming_dispatch_validation_parity_on_bad_schema(monkeypatch) -> None:
    """A schema-invalid result in the stream raises exactly like the captured path."""
    bad = {"build_status": "SUCCESS", "commit_sha": "abc"}  # no branch_name
    lines = [_stream_result_event(_wrap(bad))]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))
    with pytest.raises(SchemaValidationError):
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)


def test_streaming_dispatch_is_error_result_raises(monkeypatch) -> None:
    """A terminal result event with is_error=true surfaces as AgentDispatchError."""
    lines = list(_STREAM_PREAMBLE) + [_stream_result_event("hit limit", is_error=True)]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))
    with pytest.raises(AgentDispatchError, match="reported an error"):
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)


def test_streaming_dispatch_nonzero_exit_raises(monkeypatch) -> None:
    """A non-zero exit from a streamed agent raises AgentDispatchError."""
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: _FakePopen([], returncode=1, stderr="boom"),
    )
    with pytest.raises(AgentDispatchError, match="boom"):
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)


class _BlockingStdout:
    """A stdout that blocks on read until the process is ``kill()``-ed (EOF then).

    Models a stalled agent that stops emitting yet never closes stdout — the
    case the watchdog must rescue, since iterating such a stream blocks forever.
    """

    def __init__(self) -> None:
        self.released = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.released.wait(timeout=5)  # unblocked by _FakePopen.kill()
        raise StopIteration


def test_streaming_dispatch_timeout_raises(monkeypatch) -> None:
    """A stalled stream is killed by the watchdog and surfaces as AgentDispatchError."""
    blocking = _BlockingStdout()
    fake = _FakePopen([])
    fake.stdout = blocking

    original_kill = fake.kill

    def kill_and_release() -> None:
        original_kill()
        blocking.released.set()  # the kill closes stdout → loop ends

    fake.kill = kill_and_release  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    with pytest.raises(AgentDispatchError, match="timed out"):
        # A tiny timeout makes the watchdog fire promptly; no real agent stalls.
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, timeout=0.2)
    assert fake.killed  # the hung child is killed


def test_streaming_dispatch_late_watchdog_does_not_false_timeout(monkeypatch) -> None:
    """A watchdog firing after the stream is fully read must not flag a timeout."""
    fake = _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])
    # wait() lingers so the (tiny-timeout) watchdog fires while we are reaping,
    # i.e. after the read loop already drained stdout — the race the lock guards.
    fake.wait_delay = 0.3
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, timeout=0.05)
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.cost_usd == 0.42  # the valid result is kept, not discarded
    assert not fake.killed  # a completed child is never killed by the watchdog


def test_streaming_dispatch_falls_back_when_no_result_event(monkeypatch) -> None:
    """A streaming cmd whose output carries no result event degrades to captured parsing."""
    # No line is a `type==result` event; the wrapped block arrives as plain text.
    lines = ["agent prose\n", f"{RESULT_START_MARKER}\n",
             json.dumps(_VALID_BUILD) + "\n", f"{RESULT_END_MARKER}\n"]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"
    assert result.usage is None  # no envelope → no usage, but the run still succeeds


def test_non_streaming_cmd_uses_captured_run_not_popen(monkeypatch) -> None:
    """A non-stream-json SDLC_AGENT_CMD keeps the captured subprocess.run path."""
    def boom(*a, **kw):
        raise AssertionError("Popen must not be used for a non-streaming command")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_BUILD))
    )
    result = dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert result.data["branch_name"] == "feature/7.3-001"


def test_default_streaming_dispatch_uses_default_cmd(monkeypatch) -> None:
    """With no agent_cmd the default (streaming) command launches via Popen."""
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    dispatch_agent("build", "prompt")
    assert seen["cmd"][0]  # a non-empty executable name
    assert "stream-json" in seen["cmd"]
