# ABOUTME: The agent-dispatch boundary — shells out to Claude Code and validates output.
# ABOUTME: Story 7.3-001 — the single seam tests mock so no real agent is invoked.

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdlc.contracts import parse_and_validate

# Default command the controller shells out to. The prompt is delivered on
# stdin. The agent runs **headless** (`-p`), where there is no human to approve
# tool calls — so `--dangerously-skip-permissions` lets the dispatched agent
# actually write files, commit, and call `gh` instead of being silently
# denied (R7). Override the whole command with the ``SDLC_AGENT_CMD`` env var to
# tune the permission posture per environment, e.g.:
#   SDLC_AGENT_CMD="claude -p --permission-mode acceptEdits --allowedTools Edit,Write,Bash"
# Tests always pass an explicit ``agent_cmd`` and monkeypatch ``subprocess.run``
# so this default is never executed in CI.
#
# ``--output-format stream-json --verbose`` makes the agent emit one JSON object
# per line as it works (system/assistant/tool_use/tool_result), terminated by a
# single ``result`` event carrying the authoritative ``usage`` (token counts),
# ``total_cost_usd`` and ``session_id`` alongside the agent's text in ``result``.
# Streaming lets the controller tee live activity to the per-stage transcript
# (so ``tail -f`` shows progress) instead of waiting for the stage to finish
# (Story 11.1-001). The terminal ``result`` event has the same shape as the old
# ``--output-format json`` envelope, so usage extraction and schema validation
# are byte-for-byte identical. ``--verbose`` is required for ``claude -p`` to
# actually emit the line-delimited stream.
#
# A custom ``SDLC_AGENT_CMD`` that omits ``stream-json`` (or a non-claude agent)
# is dispatched via the captured-output path instead — dispatch consumes its
# whole stdout at once, unwraps a ``--output-format json`` envelope when present,
# and otherwise parses plain text and records no usage (graceful degradation).
DEFAULT_AGENT_CMD: list[str] = [
    "claude", "-p", "--output-format", "stream-json", "--verbose",
    "--dangerously-skip-permissions",
]


def resolve_agent_cmd(explicit: list[str] | None = None) -> list[str]:
    """The command to launch the agent: explicit arg → ``$SDLC_AGENT_CMD`` → default."""
    if explicit is not None:
        return list(explicit)
    env = os.environ.get("SDLC_AGENT_CMD")
    if env:
        return shlex.split(env)
    return list(DEFAULT_AGENT_CMD)


def _write_transcript(path: Path | None, stdout: str, stderr: str = "") -> None:
    """Persist an agent transcript (best-effort; never fails the run) — R8."""
    if path is None:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = stdout or ""
        if stderr:
            body += "\n--- stderr ---\n" + stderr
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass


class _StreamTranscript:
    """Tee stream-json lines to the per-stage transcript as they arrive (R8, 11.1-001).

    Opens the transcript once and appends each line with a flush, so ``tail -f``
    on the file shows live agent activity within ~1 s instead of only on stage
    completion. Entirely best-effort: any I/O error is swallowed so a transcript
    problem never fails the run.
    """

    def __init__(self, path: Path | None) -> None:
        self._fh = None
        if path is None:
            return
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")
        except OSError:
            self._fh = None

    def append(self, text: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(text)
            self._fh.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


def _is_streaming_cmd(cmd: list[str]) -> bool:
    """True when the command requests Claude's line-delimited ``stream-json`` output."""
    return "stream-json" in cmd


def _parse_stream_line(line: str) -> dict[str, Any] | None:
    """Parse one stream-json line into an event dict, or None when it isn't JSON.

    Unknown / non-JSON lines (blank lines, diagnostics interleaved by a custom
    agent) return None — they are still teed to the transcript but ignored for
    control flow, per the defensive-parsing note in the story.
    """
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None

# A generous default ceiling. A single story build can legitimately run for
# many minutes; the controller (not the agent) owns this timeout so a hung
# agent surfaces as a typed failure instead of blocking the run forever.
DEFAULT_TIMEOUT_S = 3600


class AgentDispatchError(Exception):
    """The agent subprocess could not be run to completion.

    Distinct from a contract error: this is an infrastructure failure (non-zero
    exit, timeout, missing executable), not a malformed-but-received response.
    """


@dataclass(frozen=True)
class AgentResult:
    """A validated agent response.

    ``data`` has already passed JSON-schema validation for ``agent_type`` so
    callers can read fields without re-checking. ``raw`` is the agent's text
    response, retained for ledger ``output_path`` logging and debugging.

    ``usage`` / ``cost_usd`` / ``session_id`` come from the ``--output-format
    json`` envelope and are None when the agent emitted plain text (a custom
    ``SDLC_AGENT_CMD`` or older ``claude``).
    """

    agent_type: str
    data: dict[str, Any]
    raw: str
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    session_id: str | None = None


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Parse a ``claude -p --output-format json`` result envelope, or None.

    Returns the envelope dict only when stdout is a single JSON object that
    looks like Claude's result envelope (``type == "result"`` with a ``result``
    field). Plain-text agent output, a non-claude agent, or malformed JSON
    return None so the caller treats stdout as the raw agent response.
    """
    text = stdout.strip()
    if not text.startswith("{"):
        return None
    try:
        env = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(env, dict) and env.get("type") == "result" and "result" in env:
        return env
    return None


def dispatch_agent(
    agent_type: str,
    prompt: str,
    *,
    story: Any | None = None,
    agent_cmd: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    transcript_path: Path | None = None,
) -> AgentResult:
    """Dispatch one agent as a subprocess and validate its response.

    The prompt is passed on the subprocess's stdin. On a clean exit the agent's
    stdout is parsed for the ``<<<RESULT_JSON>>>`` block and validated against
    the schema for ``agent_type`` (via :func:`sdlc.contracts.parse_and_validate`).

    Raises:
        AgentDispatchError: the subprocess failed to run (non-zero exit,
            timeout, executable not found).
        ResultBlockError / SchemaValidationError: the agent ran but returned a
            missing or schema-invalid result block. Callers route these to the
            bugfix loop exactly like a build failure.

    ``story`` is accepted (and ignored here) so a mock dispatcher in tests can
    key its canned responses on the story without changing this signature.
    """
    cmd = resolve_agent_cmd(agent_cmd)
    if _is_streaming_cmd(cmd):
        return _dispatch_streaming(
            agent_type, prompt, cmd, timeout=timeout, transcript_path=transcript_path
        )
    return _dispatch_captured(
        agent_type, prompt, cmd, timeout=timeout, transcript_path=transcript_path
    )


def _dispatch_captured(
    agent_type: str,
    prompt: str,
    cmd: list[str],
    *,
    timeout: int,
    transcript_path: Path | None,
) -> AgentResult:
    """Buffered dispatch: run the agent and read all of stdout at once.

    Used for a non-streaming ``SDLC_AGENT_CMD`` (no ``stream-json``) and as the
    long-standing fallback. Unwraps a ``--output-format json`` envelope when one
    is present; otherwise treats stdout as the raw agent response.
    """
    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_transcript(transcript_path, "", f"TIMEOUT after {timeout}s")
        raise AgentDispatchError(
            f"{agent_type} agent timed out after {timeout}s"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        _write_transcript(transcript_path, "", f"could not launch {cmd[0]!r}: {exc}")
        raise AgentDispatchError(
            f"could not launch {agent_type} agent ({cmd[0]!r}): {exc}"
        ) from exc

    # Persist the transcript before any interpretation, so even a non-zero exit
    # or a missing/invalid result block leaves the agent's output on disk (R8).
    _write_transcript(transcript_path, completed.stdout, completed.stderr)

    return _interpret(
        agent_type,
        completed.stdout,
        completed.stderr,
        completed.returncode,
        transcript_path,
        envelope=None,
        streaming=False,
    )


def _dispatch_streaming(
    agent_type: str,
    prompt: str,
    cmd: list[str],
    *,
    timeout: int,
    transcript_path: Path | None,
) -> AgentResult:
    """Streamed dispatch: consume stdout line-by-line, teeing each to the transcript.

    Reads the agent's ``stream-json`` output incrementally so the per-stage
    transcript reflects live activity (Story 11.1-001). The terminal ``result``
    event is captured and handed to the same interpretation logic the captured
    path uses, so usage extraction and schema validation are identical. If no
    ``result`` event arrives (the stream wasn't well-formed), interpretation
    falls back to parsing the accumulated stdout — graceful degradation rather
    than failing the run.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        _write_transcript(transcript_path, "", f"could not launch {cmd[0]!r}: {exc}")
        raise AgentDispatchError(
            f"could not launch {agent_type} agent ({cmd[0]!r}): {exc}"
        ) from exc

    # Drain stderr on a background thread so a chatty agent cannot deadlock by
    # filling the stderr pipe buffer while we are blocked reading stdout.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            chunk = proc.stderr.read()
            if chunk:
                stderr_chunks.append(chunk)
        except (OSError, ValueError):
            pass

    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    drainer.start()

    # Watchdog: reading stdout line-by-line blocks, so a stalled agent (one that
    # stops emitting but never closes stdout) would hang the read loop forever —
    # the captured path's wall-clock guarantee (``subprocess.run(timeout=…)``)
    # has no equivalent here unless we enforce it ourselves. A timer kills the
    # child at the deadline; the kill closes stdout, the loop ends, and the run
    # surfaces as a typed ``AgentDispatchError`` instead of hanging the build.
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    transcript = _StreamTranscript(transcript_path)
    raw_lines: list[str] = []
    result_event: dict[str, Any] | None = None
    try:
        if proc.stdin is not None:
            # A killed child (watchdog) breaks the stdin pipe; tolerate that so
            # the timeout surfaces below rather than as a raw BrokenPipeError.
            try:
                proc.stdin.write(prompt)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        if proc.stdout is not None:
            for line in proc.stdout:
                transcript.append(line)
                raw_lines.append(line)
                event = _parse_stream_line(line)
                if event is not None and event.get("type") == "result":
                    result_event = event
        returncode = proc.wait()
    finally:
        watchdog.cancel()

    drainer.join(timeout=1)

    if timed_out.is_set():
        transcript.append(f"\n--- TIMEOUT after {timeout}s ---\n")
        transcript.close()
        raise AgentDispatchError(
            f"{agent_type} agent timed out after {timeout}s"
        )

    drainer.join(timeout=1)
    stderr = "".join(stderr_chunks)
    if stderr:
        transcript.append("\n--- stderr ---\n" + stderr)
    transcript.close()
    raw_stdout = "".join(raw_lines)

    return _interpret(
        agent_type,
        raw_stdout,
        stderr,
        returncode,
        transcript_path,
        envelope=result_event,
        streaming=True,
    )


def _interpret(
    agent_type: str,
    stdout: str,
    stderr: str,
    returncode: int,
    transcript_path: Path | None,
    *,
    envelope: dict[str, Any] | None,
    streaming: bool,
) -> AgentResult:
    """Shared post-collection logic for both the captured and streamed paths.

    ``envelope`` is the terminal ``result`` event (streaming) or None; when None
    it is derived from ``stdout`` (captured path, or a streamed run whose result
    event never arrived — the graceful fallback). ``streaming`` suppresses the
    transcript rewrite so the live stream is preserved verbatim for ``tail -f``.
    """
    if returncode != 0:
        detail = (stderr or stdout or "").strip()
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
