# ABOUTME: Tests for the pluggable per-harness output parsers (Story 20.1-002).
# ABOUTME: Claude parity (golden), alt-parser path, and the unavailable-usage path.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sdlc import parsers as sdlc_parsers

from sdlc.contracts import (
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    ContractError,
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
    OPENCODE_PARSER_ID,
    CollectedOutput,
    OpenCodeJsonParser,
    OutputParser,
    PlainResultParser,
    UnknownParserError,
    ClaudeStreamJsonParser,
    get_parser,
    parse_opencode_export_usage,
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


# --- Story 28.1-002: the model the session actually ran on ------------------
# The ledger's `stages.model` is written from `AgentResult.model`, so the
# envelope→result wiring here is the whole live-recording path.

_MODEL_USAGE = {
    "claude-haiku-4-5": {"costUSD": 0.01, "outputTokens": 500},
    "claude-opus-4-8": {"costUSD": 11.5, "outputTokens": 74189},
}


def test_claude_parser_records_the_envelope_model() -> None:
    parser = get_parser(CLAUDE_PARSER_ID)
    env = _claude_success_envelope(_VALID_BUILD)
    env["modelUsage"] = dict(_MODEL_USAGE)
    result = parser.parse(_collected(envelope=env, streaming=True))
    # The session that carried the stage, not the cheap sub-agent inside it.
    assert result.model == "claude-opus-4-8"


def test_claude_parser_model_is_none_without_model_usage() -> None:
    # An older CLI (no `modelUsage`) must record nothing rather than guess, so
    # the model `stage_start` resolved stays in place.
    parser = get_parser(CLAUDE_PARSER_ID)
    result = parser.parse(_collected(envelope=_claude_success_envelope(_VALID_BUILD)))
    assert result.model is None


def test_plain_result_parser_reports_no_model() -> None:
    # A no-telemetry harness names no model; the registry-resolved one wins.
    result = get_parser("codex-exec").parse(_collected(stdout=_wrap(_VALID_BUILD)))
    assert result.model is None


# --- Issue #435: contract misses carry usage telemetry so the eval harness ---
# can still score tokens/cost on a run that ended in prose.


def test_claude_parser_contract_miss_carries_usage_telemetry() -> None:
    # A successful envelope (usage + cost) whose result text has no result block:
    # the raised ContractError must carry the run's telemetry so the caller can
    # still record tokens/cost instead of discarding them.
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "I finished the change but forgot to emit the result block.",
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "total_cost_usd": 0.0123,
        "session_id": "sess-1",
    }
    with pytest.raises(ContractError) as exc:
        parser.parse(_collected(envelope=env, streaming=True))
    err = exc.value
    assert err.usage == {"input_tokens": 10, "output_tokens": 20}
    assert err.cost_usd == pytest.approx(0.0123)


def test_claude_parser_contract_miss_carries_the_session_model() -> None:
    # Story 28.1-002: the same run that burned those tokens ran on a model. The
    # envelope re-ask finishes this exact row DONE and keeps its usage (Issue
    # #480 defect 1), so a model dropped here lands a DONE row with NULL model —
    # the fresh-run regression `sdlc doctor` FAILs on.
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "I finished the change but forgot to emit the result block.",
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "total_cost_usd": 0.0123,
        "session_id": "sess-1",
        "modelUsage": dict(_MODEL_USAGE),
    }
    with pytest.raises(ContractError) as exc:
        parser.parse(_collected(envelope=env, streaming=True))
    assert exc.value.model == "claude-opus-4-8"


def test_claude_parser_contract_miss_without_envelope_is_none_safe() -> None:
    # A plain-text run (no envelope) that misses the contract has no usage; the
    # telemetry attributes must be present and None rather than absent.
    parser = get_parser(CLAUDE_PARSER_ID)
    with pytest.raises(ContractError) as exc:
        parser.parse(_collected(stdout="just prose, no result block here"))
    err = exc.value
    assert getattr(err, "usage", "missing") is None
    assert getattr(err, "cost_usd", "missing") is None


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


def test_claude_parser_network_failure_is_a_plain_dispatch_error() -> None:
    # Issue #564: a wifi drop mid-dispatch exits non-zero with a transport error
    # whose text still carries limit-flavoured wording. Classifying that as a
    # throttle parked the run against a reset epoch days out, so it must fall
    # through to the ordinary dispatch error instead.
    parser = get_parser(CLAUDE_PARSER_ID)
    with pytest.raises(AgentDispatchError) as exc:
        parser.parse(
            _collected(
                returncode=1,
                stderr="API Error: Connection error. rate_limit_error? fetch failed",
            )
        )
    assert not isinstance(exc.value, RateLimitError)


def test_claude_parser_network_failure_envelope_is_a_plain_dispatch_error() -> None:
    # Same for the zero-exit error-envelope path: a transport failure is not a
    # quota verdict. A structured 429 field would still win (tested above).
    parser = get_parser(CLAUDE_PARSER_ID)
    env = {
        "type": "result",
        "result": "Connection error: getaddrinfo ENOTFOUND api.anthropic.com",
        "is_error": True,
    }
    with pytest.raises(AgentDispatchError) as exc:
        parser.parse(_collected(envelope=env, streaming=True))
    assert not isinstance(exc.value, RateLimitError)


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


# --- OpenCode `--format json` event-stream parser (Story 29.2-003) ---------
#
# Real shape verified against a live `opencode run --pure --format json
# --model anthropic/claude-haiku-4-5` (OpenCode 1.18.15, 2026-09-05): one JSON
# object per line, no header, no envelope wrapper. A "text" event's
# `part.text` carries a chunk of the assistant's response (the same text a
# `--pure` plain run would print); a "step_finish" event's `part.tokens` is
# `{total, input, output, reasoning, cache: {read, write}}` and `part.cost` is
# a float — both *per step*, not cumulative, confirmed by a two-step
# (bash-tool) run where summing every step's tokens matched
# `opencode export`'s single cumulative `info.tokens` for the same session.


def _oc_text_event(text: str, session_id: str = "ses_abc123") -> str:
    return json.dumps(
        {
            "type": "text",
            "sessionID": session_id,
            "part": {"type": "text", "text": text},
        }
    )


def _oc_step_finish_event(
    *,
    input_tok: int = 3,
    output_tok: int = 5,
    reasoning: int = 0,
    cache_write: int = 100,
    cache_read: int = 0,
    cost: float | None = 0.01,
    session_id: str = "ses_abc123",
) -> str:
    part: dict = {
        "type": "step-finish",
        "tokens": {
            "total": input_tok + output_tok + reasoning + cache_write + cache_read,
            "input": input_tok,
            "output": output_tok,
            "reasoning": reasoning,
            "cache": {"write": cache_write, "read": cache_read},
        },
    }
    if cost is not None:
        part["cost"] = cost
    return json.dumps({"type": "step_finish", "sessionID": session_id, "part": part})


def _oc_step_start_event(session_id: str = "ses_abc123") -> str:
    return json.dumps(
        {"type": "step_start", "sessionID": session_id, "part": {"type": "step-start"}}
    )


@pytest.fixture
def no_opencode_export(monkeypatch):
    """Stub the AC4 post-hoc export seam out — no OpenCode CLI, nothing recovered.

    Story 29.2-003 AC4 made ``OpenCodeJsonParser`` fall back to
    ``opencode export <sessionID>`` whenever the live stream carried no usage.
    That is a *subprocess*, and the suite must stay hermetic (no real CLI, on
    any machine, offline CI included), so every test asserting the
    "usage unavailable" outcome pins the fallback to "recovered nothing"
    explicitly rather than depending on whether the box happens to have
    OpenCode installed.
    """
    calls: list[str] = []

    def _unavailable(session_id: str) -> None:
        calls.append(session_id)
        return None

    monkeypatch.setattr(sdlc_parsers, "_opencode_export_text", _unavailable)
    return calls


def test_get_parser_resolves_opencode_json() -> None:
    parser = get_parser(OPENCODE_PARSER_ID)
    assert isinstance(parser, OpenCodeJsonParser)
    assert OPENCODE_PARSER_ID in parser_ids()


def test_opencode_parser_extracts_contract_and_sums_usage_across_steps() -> None:
    # Two steps (mirrors a bash-tool round trip): usage/cost sum, not overwrite.
    stdout = "\n".join(
        [
            _oc_step_start_event(),
            _oc_text_event("I'll run the command."),
            _oc_step_finish_event(input_tok=3, output_tok=62, cache_write=11746, cost=0.0150),
            _oc_step_start_event(),
            _oc_text_event(_wrap(_VALID_BUILD)),
            _oc_step_finish_event(
                input_tok=6, output_tok=70, cache_write=73, cache_read=11746, cost=0.0016
            ),
        ]
    )
    parser = get_parser(OPENCODE_PARSER_ID)
    result = parser.parse(_collected(stdout=stdout, agent_type="build"))

    assert result.data == _VALID_BUILD
    assert result.session_id == "ses_abc123"
    assert result.usage_available is True
    assert result.usage == {
        "input_tokens": 9,
        "output_tokens": 132,
        "cache_read_input_tokens": 11746,
        "cache_creation_input_tokens": 11819,
    }
    assert result.cost_usd == pytest.approx(0.0166, abs=1e-6)


def test_opencode_parser_folds_reasoning_tokens_into_output() -> None:
    # No dedicated ledger column for reasoning tokens; they are still generated
    # output, so they fold into output_tokens (mirrors Claude's own envelope,
    # which never separates thinking tokens out of output_tokens either) —
    # the four-key sum still reconciles against OpenCode's own `total`.
    stdout = "\n".join(
        [
            _oc_text_event(_wrap(_VALID_BUILD)),
            _oc_step_finish_event(input_tok=3, output_tok=5, reasoning=40, cache_write=0),
        ]
    )
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.usage["output_tokens"] == 45


def test_opencode_parser_missing_cost_is_none_not_fabricated() -> None:
    stdout = "\n".join(
        [
            _oc_text_event(_wrap(_VALID_BUILD)),
            _oc_step_finish_event(cost=None),
        ]
    )
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.usage is not None
    assert result.cost_usd is None


def test_opencode_parser_truncated_step_finish_degrades_usage_to_unavailable(
    no_opencode_export,
) -> None:
    # Story 29.2-003 AC3: a stream cut mid-line (e.g. a killed/truncated
    # process) must not fail the stage — the contract block already landed in
    # a complete earlier "text" line, so it is still honored; the truncated
    # step_finish line is simply unparsable JSON and is skipped, leaving no
    # usable token counts. The AC4 export fallback is stubbed out here (no
    # OpenCode on the box) so this stays the pure "nothing recovered" case.
    good_step_finish = _oc_step_finish_event()
    truncated = good_step_finish[: len(good_step_finish) // 2]
    stdout = "\n".join([_oc_text_event(_wrap(_VALID_BUILD)), truncated])

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert result.data == _VALID_BUILD
    assert result.usage is None
    assert result.cost_usd is None
    # Harness-level flag: opencode DOES support usage tracking as a class, even
    # though this particular run's stream carried no usable telemetry.
    assert result.usage_available is True


def test_opencode_parser_no_step_finish_event_is_usage_unavailable(
    no_opencode_export,
) -> None:
    # A stream with well-formed JSON throughout but simply no usage event, and
    # no export to fall back to either (AC4 stubbed unavailable).
    stdout = "\n".join([_oc_step_start_event(), _oc_text_event(_wrap(_VALID_BUILD))])
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.data == _VALID_BUILD
    assert result.usage is None
    assert result.cost_usd is None
    assert result.usage_available is True


def test_opencode_parser_non_json_stdout_degrades_to_plain_contract_path() -> None:
    # Nothing on stdout parses as NDJSON at all (e.g. an older OpenCode / a
    # non-JSON format slipped through) — fall back to reading the raw stdout
    # exactly like the plain contract path, rather than failing the stage.
    stdout = _wrap(_VALID_BUILD)
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.data == _VALID_BUILD
    assert result.usage is None
    assert result.usage_available is True


def test_opencode_parser_missing_result_block_raises_contract_error() -> None:
    stdout = "\n".join([_oc_text_event("no markers here"), _oc_step_finish_event()])
    with pytest.raises(ResultBlockError):
        get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))


def test_opencode_parser_contract_miss_carries_usage_telemetry() -> None:
    # Issue #435, now that opencode is a usage-tracking harness: a run that
    # burned real tokens but ended in prose must hand its telemetry to the
    # ContractError, or build.py's contract branch records a NULL-usage ledger
    # row for a stage that genuinely cost money.
    stdout = "\n".join(
        [
            _oc_text_event("I finished the change but forgot the result block."),
            _oc_step_finish_event(input_tok=3, output_tok=62, cache_write=11746, cost=0.0150),
        ]
    )
    with pytest.raises(ContractError) as exc:
        get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    err = exc.value
    assert err.usage == {
        "input_tokens": 3,
        "output_tokens": 62,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 11746,
    }
    assert err.cost_usd == pytest.approx(0.0150)
    # The harness tracks usage as a class, so the miss is not recorded as
    # "usage unavailable" (ContractError's own default) — that would contradict
    # the entry's `usage_tracking: true`.
    assert err.usage_available is True


def test_opencode_parser_contract_miss_without_usage_is_none_safe(
    no_opencode_export,
) -> None:
    # A stream with no step_finish at all: the telemetry attributes must be
    # present and None rather than fabricated as zero, mirroring the Claude
    # parser's plain-text miss.
    stdout = _oc_text_event("just prose, no result block here")
    with pytest.raises(ContractError) as exc:
        get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    err = exc.value
    assert err.usage is None
    assert err.cost_usd is None
    assert err.usage_available is True


def test_opencode_parser_nonzero_exit_is_plain_dispatch_error() -> None:
    # No rate-limit semantics for opencode (rate_limit_aware: false) — a
    # rate-limit-shaped message on a non-zero exit is still a plain error.
    with pytest.raises(AgentDispatchError) as exc:
        get_parser(OPENCODE_PARSER_ID).parse(
            _collected(
                returncode=1,
                stderr="usage limit reached. Try again later.",
            )
        )
    assert not isinstance(exc.value, RateLimitError)


def test_opencode_parser_ignores_non_dict_and_blank_lines() -> None:
    stdout = "\n".join(
        [
            "",
            "  ",
            "42",
            "[1, 2, 3]",
            _oc_text_event(_wrap(_VALID_BUILD)),
            _oc_step_finish_event(),
        ]
    )
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.data == _VALID_BUILD
    assert result.usage_available is True
    assert result.usage["input_tokens"] == 3


def test_opencode_parser_skips_event_with_no_part_object() -> None:
    # A well-formed event (real `type`, real `sessionID`) but no `part` key at
    # all — e.g. a future OpenCode event kind this parser doesn't know about
    # yet. It must be skipped like any other uninterpretable line, not raise.
    no_part_event = json.dumps({"type": "step_start", "sessionID": "ses_abc123"})
    stdout = "\n".join([no_part_event, _oc_text_event(_wrap(_VALID_BUILD)), _oc_step_finish_event()])
    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))
    assert result.data == _VALID_BUILD
    assert result.usage["input_tokens"] == 3


# --- Post-hoc `opencode export <sessionID>` recovery (Story 29.2-003 AC4) --
#
# The field finding: the export must be written to a *file* before parsing —
# piped through a parser it truncates around 64 KB — and, run without a TTY,
# it omits the human-readable header line it prints interactively. Both
# gotchas are the caller's responsibility (never pipe); this function's job is
# to never assume the header is present OR absent.


def _oc_export_json(
    *,
    input_tok: int = 9,
    output_tok: int = 132,
    reasoning: int = 0,
    cache_read: int = 11746,
    cache_write: int = 11819,
    cost: float | None = 0.01661735,
) -> str:
    info: dict = {
        "id": "ses_abc123",
        "tokens": {
            "input": input_tok,
            "output": output_tok,
            "reasoning": reasoning,
            "cache": {"read": cache_read, "write": cache_write},
        },
    }
    if cost is not None:
        info["cost"] = cost
    return json.dumps({"info": info, "messages": []})


def test_parse_opencode_export_usage_no_tty_has_no_header() -> None:
    # Verified: a piped/no-TTY `opencode export --pure` prints the JSON object
    # as the very first byte — no header line to skip.
    usage, cost = parse_opencode_export_usage(_oc_export_json())
    assert usage == {
        "input_tokens": 9,
        "output_tokens": 132,
        "cache_read_input_tokens": 11746,
        "cache_creation_input_tokens": 11819,
    }
    assert cost == pytest.approx(0.01661735)


def test_parse_opencode_export_usage_tolerates_a_leading_header_line() -> None:
    # An interactive TTY run may print a banner before the JSON; the parser
    # must not assume its absence (AC4) — it locates the JSON object directly.
    text = "Exporting session ses_abc123...\n" + _oc_export_json()
    usage, cost = parse_opencode_export_usage(text)
    assert usage is not None
    assert usage["input_tokens"] == 9
    assert cost == pytest.approx(0.01661735)


def test_parse_opencode_export_usage_folds_reasoning_into_output() -> None:
    usage, _ = parse_opencode_export_usage(_oc_export_json(output_tok=100, reasoning=25))
    assert usage["output_tokens"] == 125


def test_parse_opencode_export_usage_missing_cost_is_none() -> None:
    usage, cost = parse_opencode_export_usage(_oc_export_json(cost=None))
    assert usage is not None
    assert cost is None


def test_parse_opencode_export_usage_malformed_text_returns_none() -> None:
    usage, cost = parse_opencode_export_usage("not json at all")
    assert usage is None
    assert cost is None


def test_parse_opencode_export_usage_braces_present_but_invalid_json_returns_none() -> None:
    # The regex finds a matched `{...}` span (so it isn't the "no braces at
    # all" case above — the greedy match still needs a closing brace), but the
    # span's content is not valid JSON — e.g. a truncated export whose write
    # was cut mid-value, leaving a dangling trailing comma.
    usage, cost = parse_opencode_export_usage('{"info": {"tokens": 1,}}')
    assert usage is None
    assert cost is None


def test_parse_opencode_export_usage_missing_info_key_returns_none() -> None:
    # Valid JSON, but no `info` object — an export shape this parser does not
    # recognise. Never fabricate; degrade to (None, None) like any other miss.
    usage, cost = parse_opencode_export_usage('{"messages": []}')
    assert usage is None
    assert cost is None


# --- The export seam wired into the parser (Story 29.2-003 AC4) ------------
#
# Review finding: `parse_opencode_export_usage` shipped with zero production
# callers, so AC4's "recover it post-hoc" existed as a function nothing ever
# ran. These pin the wiring: when — and only when — the live stream yielded no
# usage but did name a session, the parser asks OpenCode itself.


def _stub_export(monkeypatch, text: str | None) -> list[str]:
    """Record every session id the parser asks to export; answer with `text`."""
    calls: list[str] = []

    def _export(session_id: str) -> str | None:
        calls.append(session_id)
        return text

    monkeypatch.setattr(sdlc_parsers, "_opencode_export_text", _export)
    return calls


def test_opencode_parser_recovers_usage_from_export_when_stream_had_none(
    monkeypatch,
) -> None:
    calls = _stub_export(monkeypatch, _oc_export_json())
    # A well-formed stream that simply carried no `step_finish` — the case AC4
    # exists for. The session id came off the `text` event.
    stdout = "\n".join([_oc_step_start_event(), _oc_text_event(_wrap(_VALID_BUILD))])

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert calls == ["ses_abc123"]
    assert result.data == _VALID_BUILD
    assert result.usage == {
        "input_tokens": 9,
        "output_tokens": 132,
        "cache_read_input_tokens": 11746,
        "cache_creation_input_tokens": 11819,
    }
    assert result.cost_usd == pytest.approx(0.01661735)
    assert result.usage_available is True


def test_opencode_parser_export_recovery_survives_a_truncated_stream(
    monkeypatch,
) -> None:
    # AC3 + AC4 together: the truncated `step_finish` is unusable, but the
    # session id from the intact `text` line is enough to get the real tally.
    good = _oc_step_finish_event()
    stdout = "\n".join([_oc_text_event(_wrap(_VALID_BUILD)), good[: len(good) // 2]])
    _stub_export(monkeypatch, _oc_export_json())

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert result.data == _VALID_BUILD
    assert result.usage["input_tokens"] == 9


def test_opencode_parser_does_not_export_when_the_stream_carried_usage(
    monkeypatch,
) -> None:
    # The export is a *fallback*, not a second opinion: a stream that already
    # reported its tokens must not spawn a subprocess, and must keep its own
    # figures rather than being overwritten by the export's.
    calls = _stub_export(monkeypatch, _oc_export_json())
    stdout = "\n".join(
        [_oc_text_event(_wrap(_VALID_BUILD)), _oc_step_finish_event(input_tok=3)]
    )

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert calls == []
    assert result.usage["input_tokens"] == 3


def test_opencode_parser_does_not_export_without_a_session_id(monkeypatch) -> None:
    # The non-NDJSON degraded path never learns a session id, so there is
    # nothing to export — don't guess, don't shell out.
    calls = _stub_export(monkeypatch, _oc_export_json())

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=_wrap(_VALID_BUILD)))

    assert calls == []
    assert result.data == _VALID_BUILD
    assert result.usage is None


def test_opencode_parser_export_recovery_keeps_a_cost_the_stream_reported(
    monkeypatch,
) -> None:
    # A `step_finish` whose `tokens` is malformed but whose `cost` is valid:
    # the live cost is real money and stays, while the tokens come from the
    # export. The stream's own figure wins over the export's.
    broken_tokens = json.dumps(
        {
            "type": "step_finish",
            "sessionID": "ses_abc123",
            "part": {"type": "step-finish", "tokens": "not-an-object", "cost": 0.25},
        }
    )
    stdout = "\n".join([_oc_text_event(_wrap(_VALID_BUILD)), broken_tokens])
    _stub_export(monkeypatch, _oc_export_json())

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert result.usage["input_tokens"] == 9
    assert result.cost_usd == pytest.approx(0.25)


def test_opencode_parser_keeps_cost_when_tokens_are_malformed_and_no_export(
    no_opencode_export,
) -> None:
    # The gate is `saw_cost` alone, not `saw_usage and saw_cost`: dropping a
    # cost the harness actually reported would fabricate a silent $0 for a
    # stage that spent money — the same bug class as a NULL-usage contract miss.
    broken_tokens = json.dumps(
        {
            "type": "step_finish",
            "sessionID": "ses_abc123",
            "part": {"type": "step-finish", "tokens": None, "cost": 0.25},
        }
    )
    stdout = "\n".join([_oc_text_event(_wrap(_VALID_BUILD)), broken_tokens])

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert result.usage is None
    assert result.cost_usd == pytest.approx(0.25)


def test_opencode_parser_export_recovery_reaches_the_contract_error(
    monkeypatch,
) -> None:
    # The two fixes compose: a prose-only run whose usage had to be recovered
    # post-hoc still hands that usage to build.py's ContractError branch.
    _stub_export(monkeypatch, _oc_export_json())
    stdout = _oc_text_event("I forgot the result block entirely.")

    with pytest.raises(ContractError) as exc:
        get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert exc.value.usage["input_tokens"] == 9
    assert exc.value.cost_usd == pytest.approx(0.01661735)


def test_opencode_parser_unparseable_export_leaves_usage_unavailable(
    monkeypatch,
) -> None:
    # An export that came back as garbage recovers nothing — never a zero.
    _stub_export(monkeypatch, "opencode: no such session\n")
    stdout = "\n".join([_oc_step_start_event(), _oc_text_event(_wrap(_VALID_BUILD))])

    result = get_parser(OPENCODE_PARSER_ID).parse(_collected(stdout=stdout))

    assert result.usage is None
    assert result.cost_usd is None
    assert result.usage_available is True


# --- `_opencode_export_text`: the subprocess half of the seam ---------------


def test_opencode_export_text_reads_a_file_never_a_pipe(monkeypatch, tmp_path) -> None:
    # The field gotcha that cost real time: OpenCode truncates the export at
    # ~64 KB through a pipe. This asserts the command's stdout is bound to a
    # writable file handle, never subprocess.PIPE — a regression here would be
    # invisible until an export happened to exceed 64 KB.
    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdout"] = kwargs.get("stdout")
        seen["timeout"] = kwargs.get("timeout")
        kwargs["stdout"].write(_oc_export_json())
        return _FakeCompleted("", returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    text = sdlc_parsers._opencode_export_text("ses_abc123")

    assert seen["cmd"] == ["opencode", "export", "ses_abc123"]
    assert seen["stdout"] is not subprocess.PIPE
    assert hasattr(seen["stdout"], "write")
    # Bounded: a wedged OpenCode must not hang a stage that already finished.
    assert seen["timeout"] == sdlc_parsers._EXPORT_TIMEOUT_S
    usage, cost = parse_opencode_export_usage(text)
    assert usage["input_tokens"] == 9
    assert cost == pytest.approx(0.01661735)


def test_opencode_export_text_honors_opencode_bin(monkeypatch) -> None:
    # Same override the adapter honors (`scripts/opencode-build-adapter.sh`).
    # OpenCode's sessions live in a per-install store, so exporting with a
    # different binary than the one that ran the session finds nothing.
    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        kwargs["stdout"].write(_oc_export_json())
        return _FakeCompleted("", returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setenv("OPENCODE_BIN", "/opt/custom/opencode")

    assert sdlc_parsers._opencode_export_text("ses_abc123") is not None
    assert seen["cmd"][0] == "/opt/custom/opencode"


def test_opencode_export_text_returns_none_when_the_cli_is_absent(monkeypatch) -> None:
    def _boom(cmd, **kwargs):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert sdlc_parsers._opencode_export_text("ses_abc123") is None


def test_opencode_export_text_returns_none_on_timeout(monkeypatch) -> None:
    def _hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, sdlc_parsers._EXPORT_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert sdlc_parsers._opencode_export_text("ses_abc123") is None


def test_opencode_export_text_returns_none_on_nonzero_exit(monkeypatch) -> None:
    # An unknown session id exits non-zero; whatever partial bytes landed in the
    # file are not a usable export.
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted("", returncode=1)
    )
    assert sdlc_parsers._opencode_export_text("ses_nope") is None
