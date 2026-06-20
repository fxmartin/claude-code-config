# ABOUTME: Maps Claude stream-json events to fine-grained sub-stage progress (11.1-002).
# ABOUTME: Pure logic — mapping + coalescing/rate-limiting, no subprocess or ledger.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The small, fixed `kind` enum the dashboard and `sdlc status` render against.
# Keeping it closed (not the raw stream-json type) means the UI layer never has
# to know Claude's event vocabulary, and old/fallback runs simply carry none.
AGENT_STARTED = "agent_started"
TOOL_USE = "tool_use"
FILE_CHANGED = "file_changed"
TEST_RUN = "test_run"
MESSAGE = "message"

KINDS = frozenset({AGENT_STARTED, TOOL_USE, FILE_CHANGED, TEST_RUN, MESSAGE})

# Tools whose invocation means the agent is writing to the working tree.
_FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Substrings that mark a Bash command as a test/quality-gate run rather than an
# arbitrary shell call. Matched case-insensitively against the command text.
_TEST_MARKERS = (
    "pytest",
    "jest",
    "vitest",
    "go test",
    "cargo test",
    "bats",
    "npm test",
    "npm run test",
    "bun test",
    "make test",
    "make gate",
    "quality-gate",
)

# Keep ledger messages short — they are a one-line activity summary, not a log.
_MAX_MESSAGE = 160


@dataclass(frozen=True)
class ProgressEvent:
    """A mapped sub-stage milestone: a fixed ``kind`` + a short human message."""

    kind: str
    message: str


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 1] + "…"


def _looks_like_test(command: str) -> bool:
    low = command.lower()
    return any(marker in low for marker in _TEST_MARKERS)


def _map_tool_use(block: dict[str, Any]) -> ProgressEvent:
    """Map a single ``tool_use`` content block to a progress event."""
    name = block.get("name") or "tool"
    raw_input = block.get("input")
    inp: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}

    if name in _FILE_TOOLS:
        target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
        base = Path(str(target)).name if target else ""
        return ProgressEvent(FILE_CHANGED, f"editing {base}" if base else name)

    if name == "Bash":
        cmd = str(inp.get("command") or "").strip()
        if _looks_like_test(cmd):
            return ProgressEvent(TEST_RUN, _truncate(f"running tests: {cmd}") if cmd else "running tests")
        return ProgressEvent(TOOL_USE, _truncate(f"$ {cmd}") if cmd else "running command")

    return ProgressEvent(TOOL_USE, name)


def _text_of(content: Any) -> str:
    """Best-effort extraction of assistant prose from a content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


def map_stream_event(event: dict[str, Any]) -> list[ProgressEvent]:
    """Map one stream-json event to zero or more :class:`ProgressEvent`.

    Defensive by design (mirrors dispatch's stream parsing): an unknown event
    type, a missing ``message``, or a non-dict block yields ``[]`` so the caller
    simply records nothing. An ``assistant`` turn that invokes tools yields one
    event per tool call; a text-only turn yields a single ``message`` event.
    ``result``/``user`` (tool-result) events carry no sub-stage milestone.
    """
    etype = event.get("type")

    if etype == "system":
        return [ProgressEvent(AGENT_STARTED, "agent started")]

    if etype == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        tool_events = [
            _map_tool_use(b)
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if tool_events:
            return tool_events
        text = _text_of(content)
        return [ProgressEvent(MESSAGE, _truncate(text))] if text.strip() else []

    return []


# Map Claude stream-json usage keys to the ledger's stage column names. The
# stream and the `--output-format json` envelope share these key names, so the
# live accrual lands in the exact columns the final reconciliation overwrites.
_USAGE_KEY_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def usage_of(event: Any) -> dict[str, int] | None:
    """Extract a token-usage mapping from a stream-json event, or None (11.1-003).

    Reads ``message.usage`` (assistant turns), falling back to a top-level
    ``usage`` (the terminal ``result`` event), and renames Claude's keys to the
    ledger's stage column names. Only integer components are kept; an event with
    no usage block (system / tool_use / text) returns None so the caller accrues
    nothing. Defensive by design, mirroring the rest of the stream parsing.
    """
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    mapped = {
        col: usage[src]
        for src, col in _USAGE_KEY_MAP.items()
        if isinstance(usage.get(src), int)
    }
    return mapped or None


@dataclass
class RunningUsage:
    """Accrued token totals (+ the captured session id) for one stage attempt."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = None


class UsageAccumulator:
    """Accrue running token totals from a stream of stream-json events (11.1-003).

    Sums each assistant turn's ``message.usage`` so a mid-stage query shows tokens
    building up *during* a run, not only at the end. The terminal ``result`` event
    is deliberately ignored: dispatch hands its authoritative totals to the final
    reconciliation (``Ledger.stage_set_usage``), which overwrites the accrued
    figure — final value wins, with no double counting. Cost is not carried per
    turn in stream-json, so it accrues only at reconciliation (still a strict
    improvement over today's run-level-only total).
    """

    def __init__(self) -> None:
        self.totals = RunningUsage()

    def observe(self, event: Any) -> bool:
        """Fold one event's token usage into the running totals.

        Returns True only when actual token usage was folded in and the caller
        should persist the new running total; False otherwise, so no row is
        written before any usage exists (a session-id-only event would otherwise
        write an all-zero row that renders as a misleading "0 tokens" instead of
        "—"). A ``session_id`` is still captured internally so the *first*
        usage-bearing write carries it. ``result`` events are skipped so the live
        figure never includes the authoritative total reconciliation will set.
        """
        if not isinstance(event, dict) or event.get("type") == "result":
            return False
        session_id = event.get("session_id")
        if (
            isinstance(session_id, str)
            and session_id
            and session_id != self.totals.session_id
        ):
            self.totals.session_id = session_id
        usage = usage_of(event)
        if not usage:
            return False
        for col, value in usage.items():
            setattr(self.totals, col, getattr(self.totals, col) + value)
        return True


class ProgressCoalescer:
    """Rate-limit + de-dupe mapped events so the ledger is never flooded.

    Two guards, per the story's "de-dupe consecutive identical kinds, cap per
    second": (1) an event identical to the last *admitted* one (same kind AND
    message) is dropped, so a burst of the same activity collapses to one row;
    (2) at most ``max_per_second`` events are admitted within any rolling 1s
    window, a hard flood cap independent of dedupe. Caller supplies a monotonic
    ``now`` (seconds) so the logic stays clock-free and deterministic in tests.
    """

    def __init__(self, max_per_second: int = 5) -> None:
        self._max = max_per_second
        self._last_key: tuple[str, str] | None = None
        self._window_start: float | None = None
        self._window_count = 0

    def admit(self, event: ProgressEvent, now: float) -> bool:
        """Return True if ``event`` should be written, updating internal state."""
        key = (event.kind, event.message)
        if key == self._last_key:
            return False

        if self._window_start is None or now - self._window_start >= 1.0:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self._max:
            return False

        self._window_count += 1
        self._last_key = key
        return True
