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
