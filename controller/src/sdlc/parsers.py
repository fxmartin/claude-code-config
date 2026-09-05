# ABOUTME: Pluggable per-harness output parsers — interpret an agent's collected
# ABOUTME: stdout into a validated AgentResult. Story 20.1-002, registered by id.

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sdlc.contracts import ContractError, parse_and_validate
from sdlc.progress import dominant_model
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

# The canonical id of the OpenCode `--format json` event-stream parser. Mirrors
# the parser id `harnesses.yaml`'s `opencode` entry declares (Story 29.2-003).
OPENCODE_PARSER_ID = "opencode-json"


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


def _validate_with_telemetry(
    agent_type: str,
    response: str,
    *,
    usage: dict[str, Any] | None,
    cost_usd: float | None,
    usage_available: bool,
    model: str | None = None,
) -> dict[str, Any]:
    """``parse_and_validate`` but tag a contract miss with the run's telemetry.

    Issue #435: a live eval run that ends in prose fails the result-block
    contract, and the raised :class:`~sdlc.contracts.ContractError` used to carry
    no usage — so the harness discarded the run's real tokens/cost. Here the
    exception is re-raised unchanged in type/message, with ``usage``/``cost_usd``
    attached as optional attributes so a caller (the eval harness) can still score
    the miss. The pipeline path is unaffected: it never reads these attributes and
    the exception type is identical, so ``except ContractError`` behaviour is the
    same. The attributes are always set (``None`` when a run carried no usage) so
    a reader never has to distinguish "absent" from "None".

    Story 28.1-002 attaches ``model`` for the same reason: the miss is recorded
    on the attempt's own ledger row (and the envelope re-ask finishes that exact
    row DONE), so dropping the model here would land a DONE row with a NULL
    ``stages.model`` — the fresh-run regression ``sdlc doctor`` flags.
    """
    try:
        return parse_and_validate(agent_type, response)
    except ContractError as exc:
        exc.usage = usage
        exc.cost_usd = cost_usd
        exc.usage_available = usage_available
        exc.model = model
        raise


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
            # Story 28.1-002: the model the session actually ran on, so the
            # ledger records fact rather than the pre-dispatch prediction (which
            # is None whenever model routing is off). Resolved before validation
            # so a contract miss carries it too — that attempt burned tokens on
            # this model just as much as a valid one did.
            observed_model = dominant_model(envelope.get("modelUsage"))
            data = _validate_with_telemetry(
                agent_type, agent_text,
                usage=usage, cost_usd=cost, usage_available=True,
                model=observed_model,
            )
            return AgentResult(
                agent_type=agent_type, data=data, raw=agent_text,
                usage=usage, cost_usd=cost, session_id=session_id,
                model=observed_model,
            )

        # Fallback: plain-text agent output (custom SDLC_AGENT_CMD / older claude, or
        # a streamed run that produced no result event). No envelope → no usage, so
        # a contract miss here carries None telemetry (issue #435, None-safe).
        data = _validate_with_telemetry(
            agent_type, stdout, usage=None, cost_usd=None, usage_available=True,
        )
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


def _sum_int(*values: Any) -> int:
    return sum(int(v) for v in values if isinstance(v, (int, float)))


class OpenCodeJsonParser(OutputParser):
    """Parser for `opencode run --format json`'s NDJSON event stream (Story 29.2-003).

    Verified against a live OpenCode 1.18.15 run (hosted and local oMLX
    models, 2026-09-05): one JSON object per line, no envelope, no header. Two
    event types matter here — a ``"text"`` event's ``part.text`` is a chunk of
    the assistant's response (concatenated in order, the events reconstruct
    the same prose a `--pure` plain run would print, including the
    ``<<<RESULT_JSON>>>`` contract block), and a ``"step_finish"`` event's
    ``part.tokens`` is ``{total, input, output, reasoning, cache: {read,
    write}}`` with ``part.cost`` alongside it. Confirmed **per-step, not
    cumulative**: a two-step (bash-tool) run's per-step tokens/cost summed to
    exactly match ``opencode export``'s single cumulative total for the same
    session, so every ``step_finish`` in the stream is accumulated here rather
    than the last one winning.

    ``reasoning`` tokens have no dedicated ledger column (the four canonical
    usage keys — mirrored from Claude's own envelope — are input/output/
    cache-read/cache-creation). They fold into ``output_tokens`` the same way
    Claude's envelope never separates thinking tokens out of ``output_tokens``
    either, so the four-key sum still reconciles against OpenCode's own
    reported ``total``.

    A line that fails to parse as JSON (a truncated or malformed stream) is
    skipped rather than failing the stage (AC3) — the NDJSON format is
    line-delimited, so one bad line never voids an already-complete line
    elsewhere in the same stream. When *no* line parses as JSON at all, stdout
    is treated as plain text and handed to the contract parser directly — the
    same degraded path :class:`PlainResultParser` takes — so an unexpected
    non-JSON stream (an older OpenCode, or a misconfigured `--format`) still
    honors the result block instead of failing outright. A stream that yielded
    no usable ``step_finish`` tokens but did name its session falls back to
    asking OpenCode itself (``opencode export <sessionID>``, AC4) before giving
    up; if that recovers nothing either, usage is recorded as *unavailable* and
    never fabricated as zero. ``usage_available`` itself stays ``True``
    throughout because it marks the harness *class* as usage-tracking capable,
    mirroring the Claude parser's plain-text fallback
    (``usage_available=True``, ``usage=None``).

    Maps no rate-limit or context-overflow semantics (``rate_limit_aware:
    false``), so — like :class:`PlainResultParser` — a non-zero exit is always
    a plain :class:`AgentDispatchError`, never a fabricated 429. That is a
    property of *this parser*, not a claim about the CLI: OpenCode 1.18.15 does
    define a ``ContextOverflowError`` of its own, but it surfaces as a generic
    ``session.error`` with exit 1 and no distinguishable marker on stdout, so an
    overflow lands here as an ``AgentDispatchError`` and burns a bugfix attempt
    instead of failing fast the way the Claude path does. Recognising it needs
    its own field-verified error shape and is out of scope for Story 29.2-003.
    """

    id = OPENCODE_PARSER_ID

    def parse(self, output: CollectedOutput) -> AgentResult:
        if output.returncode != 0:
            detail = (output.stderr or output.stdout or "").strip()
            raise AgentDispatchError(
                f"{output.agent_type} agent exited {output.returncode}: {detail}"
            )

        texts: list[str] = []
        session_id: str | None = None
        input_tokens = output_tokens = cache_read_tokens = cache_creation_tokens = 0
        cost_total = 0.0
        saw_json = False
        saw_usage = False
        saw_cost = False

        for line in output.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            # Require a string `type` field, OpenCode's own event discriminator,
            # not just "this line happens to be valid JSON" — a plain-text
            # response's <<<RESULT_JSON>>> body is itself a single-line JSON
            # object with no `type` key, and must not be mistaken for a real
            # NDJSON event (that would wrongly discard the surrounding prose).
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                continue
            saw_json = True
            if session_id is None:
                # Type-guarded like every other read in this loop, and for a
                # sharper reason: `sessionID` is the one value that leaves the
                # parser and becomes both an argv word (`opencode export
                # <sessionID>`, below) and a ledger column. A non-string there
                # would raise TypeError out of `subprocess.run` — which
                # `_opencode_export_text`'s OSError/SubprocessError guard does
                # not catch — and lose a stage whose real work and contract
                # block already succeeded, to a *bonus* telemetry lookup.
                # A malformed id is simply "no session id" (skip the export),
                # and a later well-formed event can still supply one.
                candidate = event.get("sessionID")
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            if event.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif event.get("type") == "step_finish":
                tokens = part.get("tokens")
                if isinstance(tokens, dict):
                    cache = tokens.get("cache")
                    cache = cache if isinstance(cache, dict) else {}
                    step_input = _sum_int(tokens.get("input"))
                    step_output = _sum_int(tokens.get("output"), tokens.get("reasoning"))
                    step_cache_read = _sum_int(cache.get("read"))
                    step_cache_write = _sum_int(cache.get("write"))
                    # An all-zero step is absent usage, not a real zero. OpenCode
                    # emits this shape when a provider reports nothing (seen with
                    # ``reason: "unknown"``). Counting it would write zeros where
                    # the columns must stay NULL — the arm then renders as *free*
                    # rather than "—", ``usage_unavailable`` stops degrading, and
                    # the AC4 ``opencode export`` recovery is short-circuited by
                    # ``usage is not None``. Story 31.2-002's rule at the source:
                    # never a 0 that reads as free. The cost gate stays separate,
                    # so a genuine ``cost: 0`` on a not-metered local model still
                    # records beside real tokens.
                    if step_input or step_output or step_cache_read or step_cache_write:
                        saw_usage = True
                        input_tokens += step_input
                        output_tokens += step_output
                        cache_read_tokens += step_cache_read
                        cache_creation_tokens += step_cache_write
                cost = part.get("cost")
                if isinstance(cost, (int, float)):
                    saw_cost = True
                    cost_total += float(cost)

        # Degrade to the plain-contract path (AC3) when nothing on stdout
        # parsed as NDJSON at all — the raw stdout might still be a valid
        # plain-text agent response.
        response_text = "\n".join(texts) if saw_json else output.stdout

        if not output.streaming:
            _write_transcript(output.transcript_path, response_text, output.stderr)

        usage: dict[str, Any] | None = None
        if saw_usage:
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_creation_input_tokens": cache_creation_tokens,
            }
        # Gated on ``saw_cost`` alone, not on ``saw_usage`` too: a
        # ``step_finish`` carrying a valid ``cost`` beside a malformed ``tokens``
        # object still reports money the run really spent, and dropping it would
        # be the same fabrication (a silent $0) this parser refuses everywhere
        # else. ``_record_stage_usage`` already persists a cost-only row —
        # ``result.usage or {}`` leaves the four token columns NULL.
        cost = cost_total if saw_cost else None

        # Story 29.2-003 AC4 — the post-hoc fallback, and the only production
        # caller of :func:`parse_opencode_export_usage`. When the live stream
        # yielded no usage (truncated mid-line, or an OpenCode build that emits
        # no ``step_finish`` at all) but did name its session, OpenCode itself
        # still holds the final tally: ask it. Best-effort by construction — a
        # failed or absent export leaves ``usage`` None, exactly as before, and
        # never fails a stage whose real work already succeeded. A cost the live
        # stream *did* report wins over the export's, since it is the figure the
        # rest of this result was built from.
        if usage is None and session_id is not None:
            export_text = _opencode_export_text(session_id)
            if export_text is not None:
                usage, export_cost = parse_opencode_export_usage(export_text)
                cost = cost if cost is not None else export_cost

        # Issue #435, and the reason this cannot reuse the bare
        # ``parse_and_validate`` the no-telemetry PlainResultParser calls: a run
        # that ends in prose still burned every token the stream reported, and
        # ``build.py``'s ContractError branch reads exactly these attributes off
        # the exception to record them on the attempt's ledger row. Validating
        # without them would land a NULL-usage row for a stage that really cost
        # money — and ContractError's own ``usage_available`` default is False,
        # which would contradict this entry's ``usage_tracking: true``.
        data = _validate_with_telemetry(
            output.agent_type,
            response_text,
            usage=usage,
            cost_usd=cost,
            usage_available=True,
        )

        return AgentResult(
            agent_type=output.agent_type,
            data=data,
            raw=response_text,
            usage=usage,
            cost_usd=cost,
            session_id=session_id,
            usage_available=True,
        )


# A bare JSON object, greedy across newlines — used to locate the JSON payload
# inside `opencode export` output regardless of whether a header line precedes
# it (see `parse_opencode_export_usage` below).
_EXPORT_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_opencode_export_usage(
    export_text: str,
) -> tuple[dict[str, Any] | None, float | None]:
    """Recover token/cost usage from `opencode export <sessionID>` text (Story 29.2-003 AC4).

    The post-hoc fallback for a stage whose live `--format json` stream yielded
    no usage (or was never captured with `--format json` at all): the session
    id is enough to ask OpenCode itself for the final tally after the fact.

    Two OpenCode 1.18.15 gotchas are the CALLER's responsibility, not this
    function's — they cost real time to discover (2026-09-05, FX, oMLX
    benchmark session), and :func:`_opencode_export_text` (the in-repo caller,
    used by :meth:`OpenCodeJsonParser.parse`) honours both:

    - The export **truncates at ~64 KB through a pipe**. Always redirect
      `opencode export <sessionID> > file` and read the file; never pipe the
      command's output straight into a parser.
    - Run **without a TTY** (any non-interactive caller), `export` omits the
      human-readable header line it prints interactively. This function must
      therefore never assume that line is present OR absent — it locates the
      JSON object directly with a regex rather than skipping a fixed number of
      leading lines, so it round-trips both an interactive capture (header +
      JSON) and a scripted one (JSON only).

    Returns ``(usage, cost_usd)`` — both ``None`` when the text carries no
    parseable export JSON, mirroring the rest of the module's "never fabricate"
    convention. ``usage`` uses the same four canonical keys the live NDJSON
    parser produces (reasoning tokens folded into ``output_tokens``).
    """
    match = _EXPORT_JSON_RE.search(export_text)
    if match is None:
        return None, None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None, None
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None, None

    usage: dict[str, Any] | None = None
    tokens = info.get("tokens")
    if isinstance(tokens, dict):
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        usage = {
            "input_tokens": _sum_int(tokens.get("input")),
            "output_tokens": _sum_int(tokens.get("output"), tokens.get("reasoning")),
            "cache_read_input_tokens": _sum_int(cache.get("read")),
            "cache_creation_input_tokens": _sum_int(cache.get("write")),
        }

    cost = info.get("cost")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    return usage, cost_usd


# Wall-clock ceiling on a post-hoc `opencode export`. Deliberately short: by the
# time this runs the stage's real work is finished and parsed, so the export is
# pure bonus telemetry — a wedged OpenCode must never convert a completed stage
# into a hung one. (The registry entry documents OpenCode's habit of blocking
# forever on an unreachable provider; the captured dispatch path has no stall
# detector, so an unbounded call here would inherit DEFAULT_TIMEOUT_S = 3600.)
_EXPORT_TIMEOUT_S = 30.0


def _opencode_export_text(session_id: str) -> str | None:
    """Capture `opencode export <session_id>` to a file and return its text.

    The seam that makes Story 29.2-003 AC4 real rather than theoretical: it is
    what :meth:`OpenCodeJsonParser.parse` calls when a run's live event stream
    carried no usage. Split out from :func:`parse_opencode_export_usage` (which
    stays pure) so the recovery path is injectable in tests without a real CLI.

    Writes to a temp **file** and reads the file back rather than reading the
    command's stdout through a pipe: OpenCode 1.18.15 truncates the export at
    ~64 KB through a pipe, and a truncated export is invalid JSON that recovers
    nothing at all. No TTY is involved, so the export omits its header line —
    harmless, since the parser locates the JSON object rather than skipping a
    fixed prefix.

    Resolves the executable through ``OPENCODE_BIN`` exactly as
    ``scripts/opencode-build-adapter.sh`` does. Not cosmetic: OpenCode keeps its
    sessions in a per-install store, so exporting from a *different* binary than
    the one that ran the session finds no session at all.

    Best-effort by contract: OpenCode not installed, a non-zero exit, a timeout
    or an unreadable file all return ``None``. Never raises — the caller already
    holds a fully parsed result and must not lose it to a failed bonus lookup.
    """
    opencode_bin = os.environ.get("OPENCODE_BIN") or "opencode"
    with tempfile.TemporaryDirectory(prefix="opencode-export-") as tmp:
        dest = Path(tmp) / "export.json"
        try:
            with dest.open("w", encoding="utf-8") as handle:
                completed = subprocess.run(
                    [opencode_bin, "export", session_id],
                    stdout=handle,
                    stderr=subprocess.DEVNULL,
                    timeout=_EXPORT_TIMEOUT_S,
                    check=False,
                )
            if completed.returncode != 0:
                return None
            return dest.read_text(encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return None


# Registry of parsers by id. A harness's `parser` field in `harnesses.yaml` names
# one of these keys; adding a harness that reuses an existing parser shape needs
# no new code here. The `codex-exec` parser is the no-telemetry plain parser the
# Codex adapter (Feature 20.3) builds on; `opencode-json` is the OpenCode
# `--format json` event-stream parser (Story 29.2-003).
_REGISTRY: dict[str, OutputParser] = {
    CLAUDE_PARSER_ID: ClaudeStreamJsonParser(),
    "codex-exec": PlainResultParser("codex-exec"),
    OPENCODE_PARSER_ID: OpenCodeJsonParser(),
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
