# ABOUTME: Tests for the stream-json → sub-stage progress mapping + coalescing (11.1-002).
# ABOUTME: Pure logic, no subprocess/ledger — maps fixture events and rate-limits them.

from __future__ import annotations

from sdlc.progress import (
    AGENT_STARTED,
    FILE_CHANGED,
    MESSAGE,
    TEST_RUN,
    TOOL_USE,
    ProgressCoalescer,
    ProgressEvent,
    map_stream_event,
)


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _tool_use(name: str, **inp: object) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


# --- mapping ---------------------------------------------------------------


def test_system_event_maps_to_agent_started() -> None:
    events = map_stream_event({"type": "system", "subtype": "init"})
    assert [e.kind for e in events] == [AGENT_STARTED]


def test_edit_tool_maps_to_file_changed_with_basename() -> None:
    events = map_stream_event(_assistant(_tool_use("Edit", file_path="src/sdlc/cli.py")))
    assert len(events) == 1
    assert events[0].kind == FILE_CHANGED
    assert "cli.py" in events[0].message
    # The absolute/relative directory is dropped — only the basename is shown.
    assert "src/sdlc" not in events[0].message


def test_write_and_notebook_tools_map_to_file_changed() -> None:
    write = map_stream_event(_assistant(_tool_use("Write", file_path="/tmp/a.py")))
    nb = map_stream_event(_assistant(_tool_use("NotebookEdit", notebook_path="/n/x.ipynb")))
    assert write[0].kind == FILE_CHANGED and "a.py" in write[0].message
    assert nb[0].kind == FILE_CHANGED and "x.ipynb" in nb[0].message


def test_bash_test_command_maps_to_test_run() -> None:
    events = map_stream_event(_assistant(_tool_use("Bash", command="uv run pytest -q")))
    assert events[0].kind == TEST_RUN


def test_bash_non_test_command_maps_to_tool_use() -> None:
    events = map_stream_event(_assistant(_tool_use("Bash", command="git status")))
    assert events[0].kind == TOOL_USE
    assert "git status" in events[0].message


def test_read_tool_maps_to_generic_tool_use() -> None:
    events = map_stream_event(_assistant(_tool_use("Read", file_path="x.py")))
    assert events[0].kind == TOOL_USE
    assert "Read" in events[0].message


def test_assistant_multiple_tools_yields_one_event_each() -> None:
    events = map_stream_event(
        _assistant(
            _tool_use("Edit", file_path="a.py"),
            _tool_use("Bash", command="pytest"),
        )
    )
    assert [e.kind for e in events] == [FILE_CHANGED, TEST_RUN]


def test_assistant_text_only_maps_to_message() -> None:
    events = map_stream_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "planning the change"}]}}
    )
    assert events[0].kind == MESSAGE
    assert "planning" in events[0].message


def test_assistant_string_content_maps_to_message() -> None:
    # Some streams carry a plain-string content rather than a block list.
    events = map_stream_event({"type": "assistant", "message": {"content": "thinking"}})
    assert events[0].kind == MESSAGE


def test_assistant_with_tools_suppresses_text_noise() -> None:
    events = map_stream_event(
        _assistant(
            {"type": "text", "text": "let me edit"},
            _tool_use("Edit", file_path="a.py"),
        )
    )
    assert [e.kind for e in events] == [FILE_CHANGED]


def test_result_and_user_events_yield_nothing() -> None:
    assert map_stream_event({"type": "result", "result": "done"}) == []
    assert map_stream_event({"type": "user", "message": {"content": "tool result"}}) == []


def test_unknown_or_malformed_event_yields_nothing() -> None:
    assert map_stream_event({"type": "mystery"}) == []
    assert map_stream_event({}) == []
    assert map_stream_event({"type": "assistant"}) == []


def test_long_message_is_truncated() -> None:
    big = "x" * 500
    events = map_stream_event(_assistant(_tool_use("Bash", command=big)))
    assert len(events[0].message) <= 160


# --- coalescing / rate limiting -------------------------------------------


def test_consecutive_identical_events_are_deduped() -> None:
    c = ProgressCoalescer()
    e = ProgressEvent(FILE_CHANGED, "editing a.py")
    assert c.admit(e, now=0.0) is True
    assert c.admit(e, now=0.01) is False  # identical to last → dropped
    assert c.admit(e, now=0.02) is False


def test_different_messages_same_kind_are_kept() -> None:
    c = ProgressCoalescer()
    assert c.admit(ProgressEvent(FILE_CHANGED, "editing a.py"), now=0.0) is True
    assert c.admit(ProgressEvent(FILE_CHANGED, "editing b.py"), now=0.0) is True


def test_per_second_cap_drops_floods() -> None:
    c = ProgressCoalescer(max_per_second=3)
    admitted = sum(
        c.admit(ProgressEvent(TOOL_USE, f"cmd {i}"), now=0.0) for i in range(10)
    )
    assert admitted == 3  # capped within the 1s window


def test_window_resets_after_one_second() -> None:
    c = ProgressCoalescer(max_per_second=2)
    assert c.admit(ProgressEvent(TOOL_USE, "a"), now=0.0) is True
    assert c.admit(ProgressEvent(TOOL_USE, "b"), now=0.0) is True
    assert c.admit(ProgressEvent(TOOL_USE, "c"), now=0.5) is False  # still in window
    # New window opens at >= 1.0s → admits again.
    assert c.admit(ProgressEvent(TOOL_USE, "d"), now=1.0) is True
