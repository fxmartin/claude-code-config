# ABOUTME: The agent-dispatch boundary — shells out to Claude Code and validates output.
# ABOUTME: Story 7.3-001 — the single seam tests mock so no real agent is invoked.

from __future__ import annotations

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
DEFAULT_AGENT_CMD: list[str] = ["claude", "-p", "--dangerously-skip-permissions"]


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
    callers can read fields without re-checking. ``raw`` is the full agent
    transcript, retained for ledger ``output_path`` logging and debugging.
    """

    agent_type: str
    data: dict[str, Any]
    raw: str


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

    data = parse_and_validate(agent_type, completed.stdout)
    return AgentResult(agent_type=agent_type, data=data, raw=completed.stdout)
