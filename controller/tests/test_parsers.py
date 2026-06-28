# ABOUTME: Tests for the pluggable per-harness output parsers (Story 20.1-002).
# ABOUTME: Claude parity (golden), alt-parser path, and the unavailable-usage path.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sdlc.contracts import (
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    ResultBlockError,
)
from sdlc.dispatch import (
    AgentDispatchError,
    AgentResult,
    ContextOverflowError,
    RateLimitError,
    dispatch_agent,
)
from sdlc.parsers import (
    CLAUDE_PARSER_ID,
    CollectedOutput,
    OutputParser,
    PlainResultParser,
    UnknownParserError,
    ClaudeStreamJsonParser,
    get_parser,
    parser_ids,
)

_VALID_BUILD = {
    "branch_name": "feature/20.1-002",
    "build_status": "SUCCESS",
    "commit_sha": "abc123",
}


def _wrap(payload: dict) -> str:
    body = json.dumps(payload)
    return f"agent prose\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


def _claude_success_envelope(payload: dict) -> dict:
    return {
        "type": "result",
        "result": _wrap(payload),
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "total_cost_usd": 0.0123,
        "session_id": "sess-1",
    }


def _collected(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
    envelope: dict | None = None,
    streaming: bool = False,
    stream_resets_at: float | None = None,
    agent_type: str = "build",
    transcript_path: Path | None = None,
) -> CollectedOutput:
    return CollectedOutput(
        agent_type=agent_type,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        transcript_path=transcript_path,
        envelope=envelope,
        streaming=streaming,
        stream_resets_at=stream_resets_at,
    )


# --- Registry --------------------------------------------------------------


def test_default_parser_is_claude() -> None:
    assert isinstance(get_parser(None), ClaudeStreamJsonParser)
    assert get_parser(None).id == CLAUDE_PARSER_ID


def test_get_parser_resolves_claude_by_id() -> None:
    assert get_parser(CLAUDE_PARSER_ID).id == CLAUDE_PARSER_ID


def test_get_parser_resolves_codex_to_plain_parser() -> None:
    parser = get_parser("codex-exec")
    assert isinstance(parser, PlainResultParser)
    assert parser.id == "codex-exec"


def test_get_parser_unknown_id_fails_fast() -> None:
    with pytest.raises(UnknownParserError) as exc:
        get_parser("no-such-parser")
    assert "no-such-parser" in str(exc.value)
    # The error lists the registered ids so the operator can correct the typo.
    assert CLAUDE_PARSER_ID in str(exc.value)


def test_every_harness_parser_id_is_registered() -> None:
    # Config↔code consistency: each parser id declared in harnesses.yaml must
    # resolve to a registered parser, or a real harness run would crash.
    config = Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    declared = {entry["parser"] for entry in raw["harnesses"].values()}
    assert declared, "harnesses.yaml declared no parsers"
    assert declared <= set(parser_ids())


# --- Claude parser parity (golden) -----------------------------------------


def test_claude_parser_extracts_contract_usage_cost_session() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    env = _claude_success_envelope(_VALID_BUILD)
    result = parser.parse(_collected(envelope=env, streaming=True))
    assert isinstance(result, AgentResult)
    assert result.data == _VALID_BUILD
    assert result.usage == {"input_tokens": 10, "output_tokens": 20}
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.session_id == "sess-1"
    # Claude is a usage-tracking harness, so usage is available even though a
    # given run may carry None — the flag reflects the harness, not the value.
    assert result.usage_available is True


def test_claude_parser_captured_path_parses_stdout_envelope() -> None:
    # envelope=None → derive it from stdout exactly like the captured path.
    parser = get_parser(CLAUDE_PARSER_ID)
    stdout = json.dumps(_claude_success_envelope(_VALID_BUILD))
    result = parser.parse(_collected(stdout=stdout))
    assert result.data == _VALID_BUILD
    assert result.session_id == "sess-1"


def test_claude_parser_plain_text_fallback_has_no_usage() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    result = parser.parse(_collected(stdout=_wrap(_VALID_BUILD)))
    assert result.data == _VALID_BUILD
    assert result.usage is None
    # A claude run that merely lacked usage is still a usage-capable harness.
    assert result.usage_available is True


def test_claude_parser_rate_limit_envelope_raises() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "Claude AI usage limit reached. Try again later.",
        "is_error": True,
    }
    with pytest.raises(RateLimitError):
        parser.parse(_collected(envelope=env, streaming=True))


def test_claude_parser_structured_429_raises_rate_limit() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "some opaque text",
        "is_error": True,
        "api_error_status": 429,
    }
    with pytest.raises(RateLimitError):
        parser.parse(_collected(envelope=env, streaming=True))


def test_claude_parser_context_overflow_raises() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "Prompt is too long: the request is ~1180341 tokens (limit 1000000)",
        "is_error": True,
    }
    with pytest.raises(ContextOverflowError):
        parser.parse(_collected(envelope=env, streaming=True))


def test_claude_parser_nonzero_exit_raises_dispatch_error() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    with pytest.raises(AgentDispatchError):
        parser.parse(_collected(returncode=1, stderr="boom"))


def test_claude_parser_nonzero_exit_rate_limit_text_raises_rate_limit() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    with pytest.raises(RateLimitError):
        parser.parse(
            _collected(
                returncode=1,
                stderr="Claude AI usage limit reached. Try again later.",
            )
        )


def test_claude_parser_fills_reset_at_from_stream_when_text_lacks_epoch() -> None:
    # Issue #120 follow-up: the session-limit text matches but carries no parseable
    # epoch, so the signal's reset_at is None. With a stream-captured resetsAt, the
    # parser fills it in so the precise resume applies on the text-matched path.
    parser = get_parser(CLAUDE_PARSER_ID)
    with pytest.raises(RateLimitError) as exc:
        parser.parse(
            _collected(
                returncode=1,
                stderr="Claude AI usage limit reached. Try again later.",
                stream_resets_at=1717171717.0,
            )
        )
    assert exc.value.signal is not None
    assert exc.value.signal.reset_at == pytest.approx(1717171717.0)


def test_claude_parser_generic_error_envelope_raises_dispatch_error() -> None:
    # An error envelope whose text is neither a rate limit nor a context overflow,
    # and that carries no structured 429 fields, falls through to a plain dispatch
    # error — not a fabricated RateLimitError or ContextOverflowError.
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "the build agent crashed unexpectedly",
        "is_error": True,
    }
    with pytest.raises(AgentDispatchError) as exc:
        parser.parse(_collected(envelope=env, streaming=True))
    assert not isinstance(exc.value, (RateLimitError, ContextOverflowError))


# --- Alt-parser path (Story AC2) -------------------------------------------


def test_plain_parser_validates_contract_against_schema() -> None:
    parser = get_parser("codex-exec")
    result = parser.parse(_collected(stdout=_wrap(_VALID_BUILD)))
    assert isinstance(result, AgentResult)
    assert result.data == _VALID_BUILD
    assert result.agent_type == "build"


def test_plain_parser_missing_result_block_raises_contract_error() -> None:
    parser = get_parser("codex-exec")
    with pytest.raises(ResultBlockError):
        parser.parse(_collected(stdout="no markers at all"))


def test_plain_parser_nonzero_exit_is_plain_dispatch_error() -> None:
    # A harness with no rate-limit semantics never raises RateLimitError, even
    # when the text resembles a throttle — no fabricated 429 handling (AC3).
    parser = get_parser("codex-exec")
    with pytest.raises(AgentDispatchError) as exc:
        parser.parse(
            _collected(
                returncode=2,
                stderr="Claude AI usage limit reached. Try again later.",
            )
        )
    assert not isinstance(exc.value, RateLimitError)


# --- Unavailable-usage path (Story AC3) ------------------------------------


def test_plain_parser_records_usage_as_unavailable_not_zero() -> None:
    parser = get_parser("codex-exec")
    result = parser.parse(_collected(stdout=_wrap(_VALID_BUILD)))
    # Not fabricated: usage stays None (the codebase's "no usage" sentinel),
    # and usage_available is explicitly False to mark the harness as untracked.
    assert result.usage is None
    assert result.cost_usd is None
    assert result.session_id is None
    assert result.usage_available is False


def test_plain_parser_ignores_claude_envelope_and_reads_marker() -> None:
    # The plain parser does not unwrap Claude's result envelope; it reads the
    # harness-neutral <<<RESULT_JSON>>> block straight out of stdout.
    parser = get_parser("codex-exec")
    result = parser.parse(_collected(stdout=_wrap(_VALID_BUILD)))
    assert result.data == _VALID_BUILD


# --- dispatch_agent wiring (the seam selects the parser) --------------------


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_dispatch_agent_uses_declared_parser(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_BUILD))
    )
    # A non-streaming custom command → captured path; parser="codex-exec" selects
    # the plain parser, which records usage as unavailable.
    result = dispatch_agent(
        "build",
        "prompt",
        agent_cmd=["codexwrap"],
        parser="codex-exec",
    )
    assert result.data == _VALID_BUILD
    assert result.usage_available is False


def test_dispatch_agent_default_parser_is_claude(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_BUILD))
    )
    # No parser arg → claude parity, usage-capable harness (backward compatible).
    result = dispatch_agent("build", "prompt", agent_cmd=["someagent"])
    assert result.data == _VALID_BUILD
    assert result.usage_available is True


def test_output_parser_is_abstract() -> None:
    with pytest.raises(TypeError):
        OutputParser()  # type: ignore[abstract]


def test_output_parser_base_parse_raises_not_implemented() -> None:
    # A subclass that delegates to the ABC's parse() hits the NotImplementedError
    # guard — the contract for any harness parser that forgets to implement parse.
    class _StubParser(OutputParser):
        id = "stub"

        def parse(self, output: CollectedOutput) -> AgentResult:
            return super().parse(output)  # type: ignore[safe-super]

    with pytest.raises(NotImplementedError):
        _StubParser().parse(_collected(stdout=_wrap(_VALID_BUILD)))
