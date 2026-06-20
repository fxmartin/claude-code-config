# ABOUTME: The agent-dispatch boundary — shells out to Claude Code and validates output.
# ABOUTME: Story 7.3-001 — the single seam tests mock so no real agent is invoked.

from __future__ import annotations

import json
import os
import shlex
import subprocess
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
# ``--output-format json`` makes the agent emit a result *envelope* on stdout —
# a single JSON object carrying the authoritative ``usage`` (token counts),
# ``total_cost_usd`` and ``session_id`` alongside the agent's text in ``result``.
# The controller unwraps it to feed the result-block parser and to record
# per-stage token/cost usage on the ledger. A custom ``SDLC_AGENT_CMD`` that
# omits the flag (or a non-claude agent) simply emits plain text — dispatch
# falls back to parsing stdout directly and records no usage.
DEFAULT_AGENT_CMD: list[str] = [
    "claude", "-p", "--output-format", "json", "--dangerously-skip-permissions",
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

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AgentDispatchError(
            f"{agent_type} agent exited {completed.returncode}: {detail}"
        )

    envelope = _parse_envelope(completed.stdout)
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
        # The raw envelope is already on disk (R8 persist above); rewrite the
        # transcript with the readable agent text so the dashboard /log view
        # shows the response, not the JSON wrapper.
        _write_transcript(transcript_path, agent_text, completed.stderr)
        data = parse_and_validate(agent_type, agent_text)
        return AgentResult(
            agent_type=agent_type, data=data, raw=agent_text,
            usage=usage, cost_usd=cost, session_id=session_id,
        )

    # Fallback: plain-text agent output (custom SDLC_AGENT_CMD / older claude).
    data = parse_and_validate(agent_type, completed.stdout)
    return AgentResult(agent_type=agent_type, data=data, raw=completed.stdout)
