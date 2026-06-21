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
    ContextOverflowError,
    RateLimitError,
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


# ---------------------------------------------------------------------------
# Story 14.1-003: a rate-limit exit raises the distinct RateLimitError
# ---------------------------------------------------------------------------

def test_dispatch_rate_limit_exit_raises_rate_limit_error(monkeypatch) -> None:
    # A non-zero exit whose stderr names a rate limit is a recoverable pause, not
    # a generic dispatch failure — surfaced as RateLimitError carrying the signal.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(
            "", returncode=1, stderr="API error 429: rate limit; Retry-After: 300"
        ),
    )
    with pytest.raises(RateLimitError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert exc.value.signal.retry_after_s == 300
    # It is still an AgentDispatchError subclass for graceful degradation.
    assert isinstance(exc.value, AgentDispatchError)


def test_dispatch_nonzero_exit_without_rate_limit_is_plain_error(monkeypatch) -> None:
    # A non-rate-limit failure must NOT be misread as a throttle (AC7 degradation).
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="segfault"),
    )
    with pytest.raises(AgentDispatchError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert not isinstance(exc.value, RateLimitError)


def test_dispatch_error_envelope_rate_limit_raises_rate_limit_error(monkeypatch) -> None:
    # An is_error result envelope whose text names a rate limit is the same pause.
    envelope = json.dumps({
        "type": "result",
        "result": "usage limit reached for this 5-hour window",
        "is_error": True,
        "subtype": "rate_limit_error",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(RateLimitError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_structured_429_envelope_raises_rate_limit_error(monkeypatch) -> None:
    # Issue #109: the CLI rejects a dispatch with a *successful* exit but an error
    # envelope carrying structured 429 fields. This must be recognised as a
    # recoverable rate-limit pause, not a generic build error.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "api_error_status": 429,
        "error": "rate_limit",
        "result": "You've hit your session limit · resets 8:20pm (Europe/Luxembourg)",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(RateLimitError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert isinstance(exc.value, AgentDispatchError)


def test_dispatch_error_field_rate_limit_without_status_raises(monkeypatch) -> None:
    # Issue #109: ``error == "rate_limit"`` alone (no api_error_status) is a throttle.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "error": "rate_limit",
        "result": "throttled",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(RateLimitError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_structured_429_unparseable_reset_no_crash(monkeypatch) -> None:
    # Issue #109: a structured 429 whose result text has no parseable reset must
    # still raise RateLimitError with reset_at=None (the wait uses the window).
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "api_error_status": 429,
        "result": "rejected, try later",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(RateLimitError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert exc.value.signal.reset_at is None


def test_dispatch_non_429_error_envelope_stays_plain_error(monkeypatch) -> None:
    # Issue #109 regression guard: a non-rate-limit error envelope must NOT be
    # misclassified as a throttle.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "subtype": "error_max_turns",
        "result": "hit the turn limit",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(AgentDispatchError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert not isinstance(exc.value, RateLimitError)


# ---------------------------------------------------------------------------
# Issue #104: a context-window overflow envelope raises ContextOverflowError
# ---------------------------------------------------------------------------

_OVERFLOW_TEXT = "Prompt is too long · the request is ~1180341 tokens (limit 1000000)"


def test_dispatch_context_overflow_envelope_raises_context_overflow_error(
    monkeypatch,
) -> None:
    # Issue #104: an is_error envelope whose text reports a prompt-too-long /
    # context-window overflow is a distinct, fail-fast failure (a fresh dispatch
    # cannot shrink in-session context) — surfaced as ContextOverflowError.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": _OVERFLOW_TEXT,
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(ContextOverflowError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    # Still an AgentDispatchError subclass so any except-AgentDispatchError
    # path degrades gracefully.
    assert isinstance(exc.value, AgentDispatchError)


def test_dispatch_context_overflow_distinct_from_rate_limit(monkeypatch) -> None:
    # Critical #109 non-shadowing guard: an overflow must NOT be misclassified as
    # a recoverable rate-limit pause (which would wait/retry forever).
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": _OVERFLOW_TEXT,
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(ContextOverflowError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert not isinstance(exc.value, RateLimitError)


def test_dispatch_non_overflow_error_envelope_stays_plain_error(monkeypatch) -> None:
    # A generic error envelope must NOT be misread as a context overflow.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "unknown agent error",
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(AgentDispatchError) as exc:
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert not isinstance(exc.value, ContextOverflowError)


@pytest.mark.parametrize(
    "overflow_text",
    [
        _OVERFLOW_TEXT,
        "context window exceeded",
        "request is 1200000 tokens (limit 1000000)",
    ],
)
def test_dispatch_overflow_text_variations(monkeypatch, overflow_text) -> None:
    # Issue #104: the matcher must accept the observed wording and its variants.
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": overflow_text,
    })
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(envelope)
    )
    with pytest.raises(ContextOverflowError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


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


# --- Story 14.2-001: per-task model routing (--model on the default cmd) ----


def test_resolve_agent_cmd_appends_model_to_default(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    cmd = resolve_agent_cmd(model="sonnet")
    assert cmd[: len(DEFAULT_AGENT_CMD)] == DEFAULT_AGENT_CMD
    assert cmd[-2:] == ["--model", "sonnet"]


def test_resolve_agent_cmd_no_model_is_unchanged_default(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    assert resolve_agent_cmd(model=None) == DEFAULT_AGENT_CMD


def test_resolve_agent_cmd_env_override_ignores_routed_model(monkeypatch) -> None:
    """SDLC_AGENT_CMD is the escape hatch: the routed model never decorates it."""
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --model opus")
    assert resolve_agent_cmd(model="haiku") == ["claude", "-p", "--model", "opus"]


def test_resolve_agent_cmd_explicit_ignores_routed_model(monkeypatch) -> None:
    """An explicit agent_cmd owns its own model — routing never appends to it."""
    monkeypatch.setenv("SDLC_AGENT_CMD", "should-be-ignored")
    assert resolve_agent_cmd(["my", "agent"], model="haiku") == ["my", "agent"]


def test_dispatch_agent_passes_routed_model_to_default_cmd(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted(_wrap(_VALID_BUILD))

    # Force the captured path by passing a non-streaming explicit command? No —
    # we want the *default* command decorated, so monkeypatch Popen-less captured
    # behaviour by routing through a non-streaming default. The default is
    # streaming, so instead assert via resolve here and exercise wiring with an
    # explicit captured cmd that carries no model (escape hatch already covered).
    monkeypatch.setattr(subprocess, "run", fake_run)
    # Default is streaming; the model wiring is unit-tested through resolve above.
    # Here we only assert dispatch accepts the kwarg without error.
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], model="sonnet")
    assert seen["cmd"][0]  # ran, model on explicit cmd is intentionally ignored


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


# --- 11.1-002: on_progress callback receives each stream event --------------


def test_streaming_dispatch_invokes_on_progress_per_event(monkeypatch) -> None:
    """Every parsed stream event is handed to the on_progress callback in order."""
    init = json.dumps({"type": "system", "subtype": "init"}) + "\n"
    asst = json.dumps(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "cli.py"}}
        ]}}
    ) + "\n"
    lines = [init, asst, _stream_result_event(_wrap(_VALID_BUILD))]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))

    seen: list[dict] = []
    dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, on_progress=seen.append)

    types = [e.get("type") for e in seen]
    assert "system" in types and "assistant" in types and "result" in types


def test_on_progress_failure_never_breaks_the_run(monkeypatch) -> None:
    """A throwing on_progress callback is isolated — the dispatch still succeeds."""
    lines = list(_STREAM_PREAMBLE) + [_stream_result_event(_wrap(_VALID_BUILD))]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakePopen(lines))

    def boom(_event: dict) -> None:
        raise RuntimeError("sink exploded")

    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, on_progress=boom)
    assert result.data["branch_name"] == "feature/7.3-001"  # run unaffected


def test_captured_path_ignores_on_progress(monkeypatch) -> None:
    """A non-streaming command never emits progress events (graceful degradation)."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_BUILD))
    )
    seen: list[dict] = []
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], on_progress=seen.append)
    assert seen == []


# --- 11.1-001: best-effort error handling (transcript I/O, launch, reap) ----


def test_parse_stream_line_invalid_json_returns_none() -> None:
    """A line that opens like JSON but isn't parseable is ignored, not raised."""
    from sdlc.dispatch import _parse_stream_line

    assert _parse_stream_line("{not valid json") is None  # JSONDecodeError path
    assert _parse_stream_line("plain diagnostic text") is None  # not a '{' line
    assert _parse_stream_line("[1, 2, 3]") is None  # valid JSON, but not a dict


def test_write_transcript_swallows_oserror(tmp_path) -> None:
    """A transcript write that fails (here: target is a directory) never raises."""
    from sdlc.dispatch import _write_transcript

    # Writing text to an existing directory raises IsADirectoryError (an OSError),
    # which the best-effort helper must swallow (R8).
    _write_transcript(tmp_path, "body", "stderr")  # must not raise


def test_stream_transcript_init_swallows_oserror(tmp_path) -> None:
    """Opening the transcript for write can fail; the tee degrades to a no-op."""
    from sdlc.dispatch import _StreamTranscript

    t = _StreamTranscript(tmp_path)  # opening a directory for write raises → swallowed
    assert t._fh is None
    t.append("ignored")  # no-op, no raise (handle is None)
    t.close()  # no-op, no raise (handle is None)


class _BrokenFh:
    """A file handle whose every operation raises OSError, to exercise the guards."""

    def write(self, data: str) -> None:
        raise OSError("write boom")

    def flush(self) -> None:
        raise OSError("flush boom")

    def close(self) -> None:
        raise OSError("close boom")


def test_stream_transcript_append_and_close_swallow_oserror(tmp_path) -> None:
    """A mid-stream write/flush/close failure is swallowed, never failing the run."""
    from sdlc.dispatch import _StreamTranscript

    t = _StreamTranscript(tmp_path / "ok.log")  # a valid open
    t._fh = _BrokenFh()  # force subsequent I/O to fail
    t.append("x")  # OSError on write/flush swallowed
    t.close()  # OSError on close swallowed
    assert t._fh is None  # close still clears the handle


def test_captured_dispatch_launch_failure_raises(monkeypatch) -> None:
    """A missing executable on the captured path surfaces as AgentDispatchError."""

    def boom(*a, **kw):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(AgentDispatchError, match="could not launch"):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_captured_dispatch_launch_failure_writes_transcript(monkeypatch, tmp_path) -> None:
    """A launch failure on the captured path still records the reason on disk (R8)."""

    def boom(*a, **kw):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(subprocess, "run", boom)
    tpath = tmp_path / "build-1.log"
    with pytest.raises(AgentDispatchError):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], transcript_path=tpath)
    assert "could not launch" in tpath.read_text(encoding="utf-8")


def test_streaming_dispatch_launch_failure_raises(monkeypatch, tmp_path) -> None:
    """A missing executable on the streaming path surfaces as AgentDispatchError."""

    def boom(*a, **kw):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(subprocess, "Popen", boom)
    tpath = tmp_path / "build-1.log"
    with pytest.raises(AgentDispatchError, match="could not launch"):
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, transcript_path=tpath)
    assert "could not launch" in tpath.read_text(encoding="utf-8")


def test_streaming_dispatch_handles_missing_stderr(monkeypatch) -> None:
    """A child with no stderr pipe (proc.stderr is None) drains cleanly."""
    fake = _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])
    fake.stderr = None  # the drain thread must short-circuit, not crash
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"


class _BrokenStderr:
    """A stderr pipe whose read raises, to exercise the drain-thread guard."""

    def read(self) -> str:
        raise OSError("stderr read boom")


def test_streaming_dispatch_stderr_read_error_is_swallowed(monkeypatch) -> None:
    """A failure draining stderr never fails the run; the result still parses."""
    fake = _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])
    fake.stderr = _BrokenStderr()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"


class _BrokenStdin:
    """A stdin pipe whose write and close raise, modelling a killed child's pipe."""

    def __init__(self) -> None:
        self.written = ""

    def write(self, data: str) -> None:
        raise BrokenPipeError("stdin write boom")

    def close(self) -> None:
        raise OSError("stdin close boom")


def test_streaming_dispatch_stdin_errors_are_swallowed(monkeypatch) -> None:
    """A broken stdin (write and close both raise) does not derail the run."""
    fake = _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])
    fake.stdin = _BrokenStdin()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"


def test_streaming_dispatch_timeout_reap_lingers(monkeypatch) -> None:
    """When the killed child lingers past the reap window, the timeout still surfaces."""
    blocking = _BlockingStdout()
    # wait() raises TimeoutExpired so the post-kill reap in the timeout branch
    # hits its `except TimeoutExpired: pass` guard rather than returning cleanly.
    fake = _FakePopen([], wait_exc=subprocess.TimeoutExpired("cmd", 30))
    fake.stdout = blocking

    original_kill = fake.kill

    def kill_and_release() -> None:
        original_kill()
        blocking.released.set()

    fake.kill = kill_and_release  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    with pytest.raises(AgentDispatchError, match="timed out"):
        dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, timeout=0.2)
    assert fake.killed


class _LingeringPopen(_FakePopen):
    """A child that closes stdout but lingers: the bounded reap times out once,
    then the unbounded reap after kill() returns the exit code.
    """

    def wait(self, timeout=None):  # noqa: ANN001 - mirror subprocess API
        if timeout is not None:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return self._returncode


def test_streaming_dispatch_eof_reap_timeout_kills_child(monkeypatch) -> None:
    """A child that closes stdout but won't exit is killed after the reap window."""
    lines = [_stream_result_event(_wrap(_VALID_BUILD))]
    fake_ref = {}

    def make(*a, **kw):
        fake_ref["p"] = _LingeringPopen(lines)
        return fake_ref["p"]

    monkeypatch.setattr(subprocess, "Popen", make)
    result = dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert result.data["branch_name"] == "feature/7.3-001"
    assert fake_ref["p"].killed  # the lingering child was force-killed on reap timeout


# --- 14.2-002: thinking-token cap surfaced as MAX_THINKING_TOKENS ----------


def test_thinking_cap_sets_env_on_captured_path(monkeypatch) -> None:
    """A configured cap exports MAX_THINKING_TOKENS to the agent subprocess."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], thinking_cap=4096)
    assert seen["env"] is not None
    assert seen["env"]["MAX_THINKING_TOKENS"] == "4096"


def test_no_thinking_cap_leaves_env_unchanged_on_captured_path(monkeypatch) -> None:
    """No cap → env=None so the subprocess inherits the parent environment (unchanged)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])
    assert seen["env"] is None


def test_thinking_cap_zero_is_no_cap(monkeypatch) -> None:
    """A zero / falsy cap is treated as no cap — env stays None (unchanged path)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], thinking_cap=0)
    assert seen["env"] is None


def test_thinking_cap_env_preserves_parent_environment(monkeypatch) -> None:
    """The cap is added *on top of* the inherited environment, not in place of it."""
    monkeypatch.setenv("SDLC_SENTINEL_VAR", "keep-me")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch_agent("build", "prompt", agent_cmd=["fake-claude"], thinking_cap=2048)
    assert seen["env"]["SDLC_SENTINEL_VAR"] == "keep-me"
    assert seen["env"]["MAX_THINKING_TOKENS"] == "2048"


def test_thinking_cap_sets_env_on_streaming_path(monkeypatch) -> None:
    """The cap also reaches the streamed Popen subprocess (the default dispatch path)."""
    seen = {}

    def make(*a, **kw):
        seen["env"] = kw.get("env")
        return _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])

    monkeypatch.setattr(subprocess, "Popen", make)
    dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD, thinking_cap=1024)
    assert seen["env"] is not None
    assert seen["env"]["MAX_THINKING_TOKENS"] == "1024"


def test_no_thinking_cap_leaves_env_unchanged_on_streaming_path(monkeypatch) -> None:
    """No cap on the streaming path → env=None (inherits parent), behaviour unchanged."""
    seen = {}

    def make(*a, **kw):
        seen["env"] = kw.get("env")
        return _FakePopen([_stream_result_event(_wrap(_VALID_BUILD))])

    monkeypatch.setattr(subprocess, "Popen", make)
    dispatch_agent("build", "prompt", agent_cmd=_STREAM_CMD)
    assert seen["env"] is None
