# ABOUTME: Pluggable per-harness output parsers — interpret an agent's collected
# ABOUTME: stdout into a validated AgentResult. Story 20.1-002, registered by id.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sdlc.contracts import parse_and_validate
from sdlc.rate_limit import RateLimitSignal, detect_rate_limit

# Imported from the dispatch boundary rather than redefined here: the typed
# errors and AgentResult are part of dispatch's public surface (callers across
# the controller already ``except AgentDispatchError`` / ``RateLimitError``), and
# the Claude-result helpers (`_parse_envelope`, `_is_context_overflow`) are the
# exact interpretation primitives this module reuses verbatim for parity. dispatch
# imports this module *lazily* (inside ``_interpret``), so importing it eagerly
# here is the one-way edge that breaks the would-be cycle.
from sdlc.dispatch import (
    AgentDispatchError,
    AgentResult,
    ContextOverflowError,
    RateLimitError,
    _is_context_overflow,
    _parse_envelope,
    _write_transcript,
)

# The canonical id of the built-in Claude harness parser. Mirrors the parser id
# the registry (`harnesses.yaml`) and `harness.py` declare for the `claude` entry.
CLAUDE_PARSER_ID = "claude-stream-json"


class UnknownParserError(Exception):
    """A harness declared an output-parser id that is not registered.

    Raised by :func:`get_parser` so an unknown parser id fails fast with an
    actionable message (the typo + the set of registered ids) rather than
    silently falling back to a parser that would mis-handle the harness output.
    """


@dataclass(frozen=True)
class CollectedOutput:
    """Everything dispatch collected from one agent run, pre-interpretation.

    This is the harness-neutral hand-off from the dispatch *collection* code
    (which runs the subprocess and reads stdout — streaming or captured) to the
    harness-specific *interpretation* code (a parser). ``envelope`` is the
    terminal Claude ``result`` event when one was captured on the streaming path,
    else ``None`` (the parser derives it from ``stdout`` when relevant).
    ``stream_resets_at`` is the absolute rate-limit reset epoch captured from a
    ``rate_limit_event`` stream line (Claude-only; ``None`` elsewhere).
    """

    agent_type: str
    stdout: str
    stderr: str
    returncode: int
    transcript_path: Path | None
    envelope: dict[str, Any] | None = None
    streaming: bool = False
    stream_resets_at: float | None = None


class OutputParser(ABC):
    """Interpret one harness's collected output into a validated AgentResult.

    Each harness declares a parser ``id`` in ``harnesses.yaml``; the controller
    resolves it through :func:`get_parser` and hands the parser a
    :class:`CollectedOutput`. The parser owns everything harness-specific —
    envelope shape, usage/cost extraction, rate-limit and context-overflow
    detection — while the ``<<<RESULT_JSON>>>`` contract it validates against
    (``sdlc.contracts.parse_and_validate``) stays harness-neutral.
    """

    id: str

    @abstractmethod
    def parse(self, output: CollectedOutput) -> AgentResult:
        """Return a validated :class:`AgentResult`, or raise a typed dispatch error."""
        raise NotImplementedError


class ClaudeStreamJsonParser(OutputParser):
    """The built-in Claude parser — `stream-json` / `--output-format json` envelope.

    This is the Claude-specific interpretation that previously lived inline in
    ``dispatch._interpret``; it is preserved byte-for-byte so the default path is
    unchanged (Story 20.1-002 AC1). It extracts the ``<<<RESULT_JSON>>>`` contract,
    ``usage``/``total_cost_usd``/``session_id``, and recognises 429/``resetsAt``
    rate-limits and "prompt is too long" context overflow.
    """

    id = CLAUDE_PARSER_ID

    def parse(self, output: CollectedOutput) -> AgentResult:
        agent_type = output.agent_type
        stdout = output.stdout
        stderr = output.stderr
        returncode = output.returncode
        transcript_path = output.transcript_path
        envelope = output.envelope
        streaming = output.streaming
        stream_resets_at = output.stream_resets_at

        def _with_stream_reset(sig: RateLimitSignal | None) -> RateLimitSignal | None:
            # Issue #120 follow-up: detect_rate_limit() recognises the session-limit
            # text but the common message carries no parseable epoch, so the matched
            # signal's reset_at is None. Fill it from the stream-captured resetsAt so
            # the precise resume applies on the text-matched path too — never override
            # an epoch the text did surface.
            if sig is not None and sig.reset_at is None and stream_resets_at is not None:
                return replace(sig, reset_at=stream_resets_at)
            return sig

        if returncode != 0:
            detail = (stderr or stdout or "").strip()
            # Story 14.1-003: a non-zero exit caused by the Max plan's rate limit is a
            # recoverable, time-based pause — not a generic dispatch failure. Surface
            # it as a distinct RateLimitError so the controller waits/parks instead of
            # burning a bugfix attempt. Absent a rate-limit signal, behaviour is today's.
            signal = _with_stream_reset(detect_rate_limit(detail))
            if signal is not None:
                raise RateLimitError(
                    f"{agent_type} agent hit the rate limit (exit {returncode}): {detail}",
                    signal=signal,
                )
            raise AgentDispatchError(
                f"{agent_type} agent exited {returncode}: {detail}"
            )

        if envelope is None:
            envelope = _parse_envelope(stdout)

        if envelope is not None:
            if envelope.get("is_error"):
                detail = (
                    envelope.get("result") or envelope.get("subtype") or "unknown error"
                )
                # Story 14.1-003: an error envelope whose subtype/text names a rate
                # limit is the same recoverable pause as a non-zero exit.
                # Issue #109: the CLI rejects a dispatch with a *successful* exit but
                # an error envelope carrying structured 429 fields
                # (``api_error_status``/``error``). Treat that as a definitive
                # rate-limit signal even when the human ``result`` text is not
                # recognised, preferring a structured reset epoch when surfaced.
                signal = _with_stream_reset(detect_rate_limit(str(detail)))
                if signal is None and (
                    envelope.get("api_error_status") == 429
                    or envelope.get("error") == "rate_limit"
                ):
                    signal = RateLimitSignal(
                        source="usage-limit", reset_at=stream_resets_at
                    )
                if signal is not None:
                    raise RateLimitError(
                        f"{agent_type} agent hit the rate limit: {detail}",
                        signal=signal,
                    )
                # Issue #104: a prompt-too-long / context-window overflow. Checked
                # AFTER the rate-limit detection so the two never shadow each other,
                # and BEFORE the generic dispatch error so the controller can
                # fail-fast instead of burning the bugfix loop on an unshrinkable
                # in-session context.
                if _is_context_overflow(str(detail)):
                    raise ContextOverflowError(
                        f"{agent_type} agent exceeded context window: {detail}"
                    )
                raise AgentDispatchError(
                    f"{agent_type} agent reported an error: {detail}"
                )
            agent_text = envelope.get("result") or ""
            usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else None
            raw_cost = envelope.get("total_cost_usd")
            cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
            session_id = envelope.get("session_id")
            # Captured path: the raw envelope is already on disk (R8 persist), so
            # rewrite the transcript with the readable agent text. Streaming path:
            # leave the verbatim stream in place — that is the live tail -f view.
            if not streaming:
                _write_transcript(transcript_path, agent_text, stderr)
            data = parse_and_validate(agent_type, agent_text)
            return AgentResult(
                agent_type=agent_type, data=data, raw=agent_text,
                usage=usage, cost_usd=cost, session_id=session_id,
            )

        # Fallback: plain-text agent output (custom SDLC_AGENT_CMD / older claude, or
        # a streamed run that produced no result event).
        data = parse_and_validate(agent_type, stdout)
        return AgentResult(agent_type=agent_type, data=data, raw=stdout)


class PlainResultParser(OutputParser):
    """Parser for a harness with a JSON contract but no usage/rate-limit semantics.

    The harness-neutral path: it reads the ``<<<RESULT_JSON>>>`` block straight
    out of stdout and validates it against the same contract schema, but it does
    **not** unwrap a Claude result envelope, and it has no rate-limit or
    context-overflow recognition (a non-zero exit is always a plain
    :class:`AgentDispatchError`, never a fabricated 429 — Story 20.1-002 AC3).
    Usage is recorded as *unavailable* (``usage_available=False``, ``usage=None``)
    rather than fabricated as zero, so the run still advances and cost tracking
    skips the stage instead of comparing against a misleading zero. This is the
    parser the `codex` adapter (Feature 20.3) and any future no-telemetry CLI
    harness declare.
    """

    def __init__(self, parser_id: str) -> None:
        self.id = parser_id

    def parse(self, output: CollectedOutput) -> AgentResult:
        if output.returncode != 0:
            detail = (output.stderr or output.stdout or "").strip()
            raise AgentDispatchError(
                f"{output.agent_type} agent exited {output.returncode}: {detail}"
            )
        # Persist the readable output before interpreting so even a contract
        # failure leaves the agent's response on disk (R8), mirroring dispatch.
        if not output.streaming:
            _write_transcript(output.transcript_path, output.stdout, output.stderr)
        data = parse_and_validate(output.agent_type, output.stdout)
        return AgentResult(
            agent_type=output.agent_type,
            data=data,
            raw=output.stdout,
            usage=None,
            cost_usd=None,
            session_id=None,
            usage_available=False,
        )


# Registry of parsers by id. A harness's `parser` field in `harnesses.yaml` names
# one of these keys; adding a harness that reuses an existing parser shape needs
# no new code here. The `codex-exec` parser is the no-telemetry plain parser the
# Codex adapter (Feature 20.3) builds on.
_REGISTRY: dict[str, OutputParser] = {
    CLAUDE_PARSER_ID: ClaudeStreamJsonParser(),
    "codex-exec": PlainResultParser("codex-exec"),
}


def parser_ids() -> tuple[str, ...]:
    """The ids of every registered parser (for config↔code consistency checks)."""
    return tuple(_REGISTRY)


def get_parser(parser_id: str | None) -> OutputParser:
    """Resolve a parser by id; ``None`` → the built-in Claude parser (default).

    Raises :class:`UnknownParserError` for an unregistered id so a misdeclared
    harness fails fast with the typo and the set of known ids, rather than
    silently mis-parsing.
    """
    if parser_id is None:
        return _REGISTRY[CLAUDE_PARSER_ID]
    try:
        return _REGISTRY[parser_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownParserError(
            f"unknown output parser {parser_id!r}; registered parsers: {known}"
        ) from None
