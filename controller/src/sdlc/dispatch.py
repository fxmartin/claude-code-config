# ABOUTME: The agent-dispatch boundary — shells out to Claude Code and validates output.
# ABOUTME: Story 7.3-001 — the single seam tests mock so no real agent is invoked.

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sdlc.rate_limit import RateLimitSignal
from sdlc.sanitize import sanitize_prompt

# A sink that receives each parsed stream-json event as it arrives (Story
# 11.1-002), used to emit fine-grained sub-stage progress to the ledger.
ProgressCallback = Callable[[dict[str, Any]], None]

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

# Story 14.2-002: the Claude Code env var that caps per-request extended-thinking
# tokens. Surfaced through dispatch so a long overnight run can bound the hidden
# thinking cost of every dispatched agent. It is honoured by `claude -p`
# regardless of the rest of the command, so it works for the built-in default and
# any `SDLC_AGENT_CMD`/explicit override alike — the cap is an environment knob,
# not a CLI flag, so it never has to be threaded into the (escape-hatch) argv.
THINKING_CAP_ENV = "MAX_THINKING_TOKENS"

# Story 13.1-001: the default deny baseline for a dispatched agent. Every agent
# runs under ``--dangerously-skip-permissions`` (no human to approve tool calls),
# which suppresses the *prompt* but does NOT disable an explicit deny list on the
# command surface. ``settings.json`` ``permissions.deny`` is bypassed by the flag;
# ``--disallowedTools`` on the ``claude`` invocation is not. So this baseline is
# wired onto the built-in command (see ``resolve_agent_cmd``) to keep a refused
# floor — secret-bearing reads/writes and "pipe the internet into a shell" egress —
# even with prompts suppressed. The rules are deliberately narrow: they block only
# the listed secret paths and egress shells, never ordinary edit/test work.
DENY_BASELINE: tuple[str, ...] = (
    "Read(~/.ssh/**)",
    "Read(~/.aws/**)",
    "Read(**/.env*)",
    "Write(~/.ssh/**)",
    "Bash(curl * | bash)",
    "Bash(ssh *)",
)

# Per-repo override for the deny baseline (AC3): set ``SDLC_DENY_BASELINE`` to a
# comma-separated rule list to *replace* the baseline for one repo without editing
# controller code, or to the empty string to opt out entirely. Unset → the
# built-in baseline above. This mirrors the ``SDLC_AGENT_CMD`` escape-hatch model:
# the operator owns the posture per environment.
DENY_BASELINE_ENV = "SDLC_DENY_BASELINE"


def resolve_deny_rules() -> list[str]:
    """The deny rules to apply to the built-in dispatch command.

    Story 13.1-001: ``$SDLC_DENY_BASELINE`` (comma-separated) replaces the
    built-in :data:`DENY_BASELINE` when set — an empty value disables the baseline
    for that repo (the documented per-repo opt-out). Whitespace around each rule is
    trimmed and blank entries are dropped, so ``"A, , B"`` yields ``["A", "B"]``.
    Unset → the built-in baseline, so default behaviour needs no configuration.
    """
    override = os.environ.get(DENY_BASELINE_ENV)
    if override is not None:
        return [rule.strip() for rule in override.split(",") if rule.strip()]
    return list(DENY_BASELINE)


def _dispatch_env(thinking_cap: int | None) -> dict[str, str]:
    """The subprocess environment for a dispatch (always a copy of the parent).

    Issue #214: every controller-dispatched agent is marked with
    ``SDLC_BATCH_BUILD=1`` so host hooks can tell a batch-build agent from an
    interactive session. The ``on-pr-merge-docs`` PostToolUse hook keys off this
    marker to stay a no-op during a build — otherwise it injects "update the docs"
    context into the dispatched merge agent, which then commits the regenerated
    build-progress render onto whatever branch is checked out (main). The marker is
    layered on top of a copy of the current environment, so it is always returned
    (never ``None``) — the dispatch must run with this env in place.

    Story 14.2-002: when a thinking-token cap is configured, export it as
    ``MAX_THINKING_TOKENS`` on top of that environment so the dispatched agent
    bounds its extended-thinking budget. A falsy cap (``0`` / ``None``) simply omits
    the cap var while still carrying the batch-build marker.

    Auto-compaction is deliberately left at Claude Code's default (enabled,
    ``autoCompactEnabled``): the controller never sets ``DISABLE_AUTO_COMPACT``,
    so long runs keep compacting context near the limit. There is no documented
    env var to lower the *threshold*, so "early compaction" here means honouring —
    not disabling — the built-in behaviour while the thinking cap does the bounding.
    """
    env = dict(os.environ)
    env["SDLC_BATCH_BUILD"] = "1"
    if thinking_cap and thinking_cap > 0:
        env[THINKING_CAP_ENV] = str(thinking_cap)
    return env


def resolve_agent_cmd(
    explicit: list[str] | None = None, *, model: str | None = None
) -> list[str]:
    """The command to launch the agent: explicit arg → ``$SDLC_AGENT_CMD`` → default.

    Story 14.2-001: ``model`` is the per-stage model the routing map selected. It
    decorates **only** the built-in default command (``--model <model>``); an
    explicit ``agent_cmd`` or a ``$SDLC_AGENT_CMD`` override is the escape hatch
    and owns its own model selection, so the routed model is deliberately ignored
    there (precedence: explicit/env > map). With no ``model`` the default command
    is byte-for-byte today's, so routing-off behaviour is unchanged.

    Story 13.1-001: the deny baseline (:func:`resolve_deny_rules`) is appended to
    the built-in command as ``--disallowedTools`` so the secret/egress floor holds
    under ``--dangerously-skip-permissions``. Like ``model``, it decorates only the
    built-in default — an explicit/env command owns its own permission posture, so
    no deny rules are appended there. An empty resolved baseline (per-repo opt-out)
    omits the flag entirely, leaving the default byte-for-byte its pre-13.1 form.
    """
    if explicit is not None:
        return list(explicit)
    env = os.environ.get("SDLC_AGENT_CMD")
    if env:
        return shlex.split(env)
    cmd = list(DEFAULT_AGENT_CMD)
    deny = resolve_deny_rules()
    if deny:
        cmd += ["--disallowedTools", ",".join(deny)]
    if model:
        cmd += ["--model", model]
    return cmd


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


def _extract_resets_at(event: dict[str, Any]) -> float | None:
    """The ``resetsAt`` epoch from a ``rate_limit_event`` stream line, or None.

    Issue #120: a structured 429 carries its absolute window-reset epoch as
    ``{"type": "rate_limit_event", "rate_limit_info": {"resetsAt": <epoch>}}``.
    Returns the epoch as a float when present and numeric; returns None on any
    malformed shape (missing ``rate_limit_info``, missing ``resetsAt``, or a
    non-numeric value) so a defensive parse can never crash the stream loop.
    """
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    resets_at = info.get("resetsAt")
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    return float(resets_at)

# A generous default ceiling. A single story build can legitimately run for
# many minutes; the controller (not the agent) owns this timeout so a hung
# agent surfaces as a typed failure instead of blocking the run forever.
DEFAULT_TIMEOUT_S = 3600

# Grace period to reap a streamed child after stdout reaches EOF. The process
# has closed stdout, so it should exit within milliseconds; this only bounds the
# pathological case of a child that lingers after EOF so a reap cannot hang.
_POST_STREAM_WAIT_S = 30

# Story 13.4-001 — kill-switch and heartbeat dead-man.
#
# When a dispatched agent must be terminated (wall-clock timeout, output-idle
# stall) the controller kills the agent's whole **process group**, not just the
# parent, so a tool subprocess the agent spawned cannot be orphaned and survive
# the kill. Termination is graceful-then-hard: SIGTERM the group first (the
# agent can flush and exit cleanly), wait ``_TERM_GRACE_S``, then SIGKILL the
# group to make termination certain.
_TERM_GRACE_S = 10.0

# Output-idle (heartbeat) stall window for the streaming path. A streamed agent
# that emits no stream-json line for this long is treated as **stalled** and
# killed with a clear message, rather than waiting out the full wall-clock
# ``timeout``. ``None`` disables stall detection (only the wall-clock guard
# applies). Generous by default: a single tool call can run for minutes without
# emitting a line, so the dead-man only fires on a genuine hang, not slow work.
DEFAULT_STALL_TIMEOUT_S = 300.0

# How often the heartbeat monitor re-checks output idleness, capped so a tiny
# stall window (tests, aggressive configs) is still detected promptly and the
# monitor thread notices a wall-clock kill and exits without lingering. Four
# wakeups/second is negligible CPU even on an hour-long run.
_HEARTBEAT_POLL_S = 0.25

# Quarantine suffix for a killed agent's transcript. The partial transcript is
# copied to ``<transcript>.killed`` so the kill can be reviewed without the next
# run overwriting the live log, and the path is named in the raised error so it
# lands in the controller's ledger event for the failed stage.
_QUARANTINE_SUFFIX = ".killed"


def _signal_process_group(proc: Any, sig: int) -> None:
    """Send ``sig`` to the agent's process group, falling back to the direct child.

    ``os.killpg(os.getpgid(pid), sig)`` reaches every process in the group — the
    agent and any tool subprocess it spawned — so orphaned children cannot
    survive (Story 13.4-001). When the group cannot be signalled (no pid yet, the
    child already reaped, a permission error, or a platform without POSIX process
    groups) it falls back to signalling just the direct child via
    ``proc.send_signal`` / ``proc.kill``. Best-effort throughout: every "already
    gone" race is swallowed because it means the work is already done.
    """
    pid = getattr(proc, "pid", None)
    if pid is not None and hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, OSError, ValueError):
        pass


def _terminate_process_group(proc: Any, *, grace_s: float = _TERM_GRACE_S) -> None:
    """Graceful-then-hard kill of the agent's whole process group (Story 13.4-001).

    SIGTERM the group first so a well-behaved agent (and its children) can exit
    cleanly, wait up to ``grace_s`` for that exit, then SIGKILL the group so a
    runaway / hung agent is terminated for certain and nothing is left orphaned.
    Both the wait and the signalling are best-effort — a child that exits on
    SIGTERM is never SIGKILLed, and a reap that times out simply proceeds to the
    next step rather than raising.
    """
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
        return  # exited cleanly on SIGTERM; no hard kill needed
    except subprocess.TimeoutExpired:
        pass
    except Exception:  # noqa: BLE001 - reaping is best-effort, never fatal
        pass
    _signal_process_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=grace_s)
    except Exception:  # noqa: BLE001 - reaping is best-effort, never fatal
        pass


def _quarantine_transcript(path: Path | None, reason: str) -> Path | None:
    """Copy a killed agent's transcript to a quarantine sibling for review (R8, 13.4-001).

    When the controller kills a stalled / runaway agent its partial transcript is
    preserved under ``<transcript>.killed`` (prefixed with the kill reason) so the
    event can be reviewed and the next run cannot silently overwrite it. Entirely
    best-effort: no transcript path, a transcript that was never written, or any
    I/O error returns ``None`` so quarantine can never itself fail the run.
    """
    if path is None:
        return None
    try:
        src = Path(path)
        if not src.exists():
            return None
        dest = src.with_name(src.name + _QUARANTINE_SUFFIX)
        body = src.read_text(encoding="utf-8")
        dest.write_text(
            f"--- QUARANTINED: {reason} ---\n{body}", encoding="utf-8"
        )
        return dest
    except OSError:
        return None


class AgentDispatchError(Exception):
    """The agent subprocess could not be run to completion.

    Distinct from a contract error: this is an infrastructure failure (non-zero
    exit, timeout, missing executable), not a malformed-but-received response.
    """


class RateLimitError(AgentDispatchError):
    """The agent failed because the Max plan's rate-limit / quota was exhausted.

    Story 14.1-003: a subclass of :class:`AgentDispatchError` so any existing
    ``except AgentDispatchError`` still degrades gracefully, but the controller's
    stage dispatch catches it *first* and treats it as a recoverable, time-based
    pause (wait-and-resume / durable park) rather than a stage ``FAILED`` that
    would burn a bugfix attempt. ``signal`` carries the detected backoff / reset
    hint so the controller can compute the window-reset time.
    """

    def __init__(self, message: str, *, signal: RateLimitSignal) -> None:
        super().__init__(message)
        self.signal = signal


class ContextOverflowError(AgentDispatchError):
    """The agent failed because its prompt exceeded the model context window.

    Issue #104: a dispatch can exit 0 yet emit an ``is_error`` envelope whose
    text is e.g. ``"Prompt is too long · the request is ~1180341 tokens (limit
    1000000)"``. That is neither a recoverable rate-limit pause nor a fixable
    stage failure — a *fresh* dispatch cannot shrink the in-session context, so
    the bugfix loop would only re-overflow. A subclass of
    :class:`AgentDispatchError` (so ``except AgentDispatchError`` still
    degrades gracefully), but the controller catches it *first* and fails the
    stage fast with ``failure_category="context-overflow"``.
    """


# Issue #104: prompt-too-long / context-window overflow signatures. Matched
# case-insensitively against the error-envelope ``result`` text. Kept distinct
# from the rate-limit matcher and applied *after* it so the two never shadow.
_CONTEXT_OVERFLOW_PATTERNS = (
    re.compile(r"\bprompt is too long\b", re.IGNORECASE),
    # Anchor on an actual token *count* before "limit", and stay within one
    # clause (no sentence break), so benign error prose that merely strings
    # together "request is … tokens … limit" across sentences cannot match.
    re.compile(
        r"\brequest is\b[^.!?\n]*?\d[\d,]*\s*tokens\b[^.!?\n]*\blimit\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcontext.*(?:window|limit).*(?:exceeded|too long)\b", re.IGNORECASE
    ),
)


def _is_context_overflow(text: str) -> bool:
    """True when ``text`` reports a prompt-too-long / context-window overflow."""
    return any(pattern.search(text) for pattern in _CONTEXT_OVERFLOW_PATTERNS)




# --- Story 13.4-002: optional container sandbox for untrusted repos --------
#
# The deny baseline (13.1-001) narrows what a dispatched agent can touch on the
# *host*; the sandbox is the stronger option for an untrusted repo — the agent
# never had host or network reach to begin with. When enabled, the resolved agent
# command is *wrapped* in a hardened ``<runtime> run`` invocation: the worktree is
# bind-mounted, egress is off, every Linux capability is dropped, privilege
# escalation is disabled, and the process runs as a non-root user matching the
# host operator. The prompt still arrives on stdin and the ``<<<RESULT_JSON>>>``
# envelope still streams out on stdout, so the result contract is byte-for-byte
# the host path's (AC2). Opt-in only — trusted local runs stay on the host (AC1).

# Opt-in to the sandbox. ``--sandbox`` threads ``sandbox=True`` down from the build
# loop; ``SDLC_SANDBOX`` (``1``/``true``/``yes``/``on``) is the per-repo / per-env
# config equivalent so an untrusted repo can default to sandboxed without the flag
# — and so a *resumed* run honours it even before the flag is re-supplied.
SANDBOX_ENV = "SDLC_SANDBOX"
# The container image the agent runs inside. It must already contain ``claude``
# (and the repo's toolchain); the controller never builds it. Default is a
# conventional name the operator is expected to have built or pulled.
SANDBOX_IMAGE_ENV = "SDLC_SANDBOX_IMAGE"
DEFAULT_SANDBOX_IMAGE = "sdlc-agent-sandbox:latest"
# Force a specific container runtime; unset → auto-detect (podman, then docker).
SANDBOX_RUNTIME_ENV = "SDLC_SANDBOX_RUNTIME"
# Network mode for the container. Default ``none`` = no egress (AC1). The operator
# can point this at a locked-down filtering network for the rare stage that
# genuinely needs the API — "explicit allowlist only if a stage needs it" — but
# the default keeps the agent fully off-network.
SANDBOX_NETWORK_ENV = "SDLC_SANDBOX_NETWORK"
DEFAULT_SANDBOX_NETWORK = "none"
# Runtimes tried, in order, when ``$SDLC_SANDBOX_RUNTIME`` is unset.
_SANDBOX_RUNTIMES: tuple[str, ...] = ("podman", "docker")
# Where the worktree is bind-mounted inside the container; the agent runs here.
_SANDBOX_WORKDIR = "/workspace"


class SandboxUnavailableError(AgentDispatchError):
    """``--sandbox`` was requested but no container runtime is available (AC3).

    A subclass of :class:`AgentDispatchError` so an existing ``except
    AgentDispatchError`` still degrades gracefully, but it is raised *before* any
    agent is launched so the controller fails fast with a clear message rather
    than silently dispatching unsandboxed on the host.
    """


def sandbox_enabled(explicit: bool | None = None) -> bool:
    """Whether the dispatched agent should run inside the container sandbox.

    The explicit flag wins both ways: ``True`` forces it on, ``False`` forces it
    off even when the env opts in (a per-repo override of the env config). ``None``
    defers to ``$SDLC_SANDBOX`` (``1``/``true``/``yes``/``on``, case-insensitive)
    so an untrusted repo can default to sandboxed without the flag. Unset → off, so
    the host path is byte-for-byte today's.
    """
    if explicit is not None:
        return explicit
    return os.environ.get(SANDBOX_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def detect_container_runtime() -> str:
    """The container runtime to use, or raise :class:`SandboxUnavailableError`.

    ``$SDLC_SANDBOX_RUNTIME`` forces a specific binary (still verified present on
    PATH); unset auto-detects ``podman`` then ``docker``. Raising here — before any
    agent is dispatched — is the fail-fast that AC3 requires, so a requested
    sandbox never silently degrades to an unsandboxed host run.
    """
    forced = os.environ.get(SANDBOX_RUNTIME_ENV, "").strip()
    for name in (forced,) if forced else _SANDBOX_RUNTIMES:
        if name and shutil.which(name):
            return name
    if forced:
        raise SandboxUnavailableError(
            f"sandbox requested but container runtime {forced!r} "
            f"(${SANDBOX_RUNTIME_ENV}) was not found on PATH"
        )
    raise SandboxUnavailableError(
        "sandbox requested but no container runtime found on PATH (looked for "
        f"{', '.join(_SANDBOX_RUNTIMES)}); install one or unset ${SANDBOX_ENV}"
    )


def sandbox_wrap(
    cmd: list[str],
    *,
    runtime: str,
    image: str,
    mount: Path,
    network: str = DEFAULT_SANDBOX_NETWORK,
    env: dict[str, str] | None = None,
    forward_env: tuple[str, ...] = (),
) -> list[str]:
    """Wrap ``cmd`` in a hardened, no-egress ``<runtime> run`` invocation (AC1).

    The worktree (``mount``) is bind-mounted at ``/workspace`` and becomes the
    agent's working directory, so branches/commits the agent makes land back in
    the host worktree and the ``<<<RESULT_JSON>>>`` envelope streams out over
    stdout exactly as on the host path (AC2 — the contract is unchanged). The
    container has no network egress (``--network none`` by default), every Linux
    capability dropped (``--cap-drop ALL``), no privilege escalation
    (``--security-opt no-new-privileges``), and a non-root user matching the host
    uid/gid so mounted files stay owned by the operator. ``-i`` keeps stdin open so
    the prompt is delivered exactly as on the host path; ``--rm`` discards the
    container after the stage. ``forward_env`` names env vars (e.g. the
    thinking-token cap) to pass through into the container from ``env``.
    """
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    argv = [
        runtime, "run", "--rm", "-i",
        "--network", network,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{gid}",
        "-v", f"{Path(mount)}:{_SANDBOX_WORKDIR}:Z",
        "-w", _SANDBOX_WORKDIR,
    ]
    source = env if env is not None else os.environ
    for key in forward_env:
        value = source.get(key)
        if value is not None:
            argv += ["-e", f"{key}={value}"]
    argv.append(image)
    argv += cmd
    return argv


def _apply_sandbox(
    cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None
) -> list[str]:
    """Resolve sandbox config and wrap ``cmd``; fail fast if no runtime (AC3).

    Reads the runtime (auto-detected or ``$SDLC_SANDBOX_RUNTIME``), image
    (``$SDLC_SANDBOX_IMAGE`` → :data:`DEFAULT_SANDBOX_IMAGE`), and network mode
    (``$SDLC_SANDBOX_NETWORK`` → ``none``) from the environment. The bind mount is
    the per-story worktree ``cwd`` (the controller's concurrency unit); ``None``
    falls back to the current directory. The thinking-token cap, when set on the
    dispatch ``env``, is forwarded into the container so the in-sandbox agent
    honours the same ``MAX_THINKING_TOKENS`` bound as the host path.
    """
    runtime = detect_container_runtime()
    image = os.environ.get(SANDBOX_IMAGE_ENV, "").strip() or DEFAULT_SANDBOX_IMAGE
    network = os.environ.get(SANDBOX_NETWORK_ENV, "").strip() or DEFAULT_SANDBOX_NETWORK
    mount = Path(cwd) if cwd is not None else Path.cwd()
    return sandbox_wrap(
        cmd, runtime=runtime, image=image, mount=mount, network=network,
        env=env, forward_env=(THINKING_CAP_ENV,),
    )


@dataclass(frozen=True)
class AgentResult:
    """A validated agent response.

    ``data`` has already passed JSON-schema validation for ``agent_type`` so
    callers can read fields without re-checking. ``raw`` is the agent's text
    response, retained for ledger ``output_path`` logging and debugging.

    ``usage`` / ``cost_usd`` / ``session_id`` come from the ``--output-format
    json`` envelope and are None when the agent emitted plain text (a custom
    ``SDLC_AGENT_CMD`` or older ``claude``).

    ``usage_available`` (Story 20.1-002) marks whether the *harness* tracks usage
    at all. The Claude parser leaves it ``True`` even when a given run carried no
    usage (the value reflects the run, the flag reflects the harness). A parser
    for a harness with no usage/rate-limit semantics (e.g. ``codex-exec``) sets it
    ``False`` so usage is recorded as *unavailable* rather than fabricated as zero.
    """

    agent_type: str
    data: dict[str, Any]
    raw: str
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    usage_available: bool = True


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
    model: str | None = None,
    thinking_cap: int | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    stall_timeout: float | None = DEFAULT_STALL_TIMEOUT_S,
    transcript_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    cwd: Path | None = None,
    sandbox: bool | None = None,
    parser: str | None = None,
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

    ``on_progress`` (Story 11.1-002) is called with each parsed stream-json event
    as it arrives on the streaming path, so a caller can emit fine-grained
    sub-stage progress to the ledger. It is never called on the captured path
    (no streaming → no sub-stage milestones); failures inside the callback are
    isolated so progress recording can never break the run.

    ``cwd`` (Story 17.2-001) is the working directory the agent subprocess runs
    in — the controller sets it to a per-story git worktree so concurrent stories
    never collide in a shared checkout. ``None`` inherits the parent's cwd, so the
    shared-root / sequential path is byte-for-byte today's.

    ``stall_timeout`` (Story 13.4-001) is the output-idle (heartbeat) dead-man for
    the streaming path: an agent that emits no stream line for this many seconds
    is killed as *stalled* with a clear message, bounding any hang well inside the
    much larger wall-clock ``timeout``. ``None`` disables it. The captured path has
    no stream to monitor, so it relies on ``timeout`` alone (unchanged).

    ``sandbox`` (Story 13.4-002) runs the agent inside a no-egress, cap-dropped,
    non-root container with the worktree (``cwd``) bind-mounted — the recommended
    path for an untrusted repo. ``True`` forces it on, ``False`` off, ``None``
    defers to ``$SDLC_SANDBOX`` (the per-repo config). When enabled the resolved
    command is wrapped in ``<runtime> run`` and, if no container runtime is
    present, dispatch fails fast with :class:`SandboxUnavailableError` rather than
    running unsandboxed (AC3). The result contract is identical to the host path
    (AC2). Default (``None`` with the env unset) is the host path, unchanged.

    ``parser`` (Story 20.1-002) is the id of the per-harness output parser used to
    interpret the agent's stdout into the validated result. ``None`` selects the
    built-in Claude parser, so the default path is byte-for-byte today's; a
    non-Claude harness passes its declared parser id (e.g. ``"codex-exec"``) so it
    gets proper handling instead of the lossy plain-stdout fallback. An
    unregistered id fails fast with :class:`~sdlc.parsers.UnknownParserError`.
    """
    cmd = resolve_agent_cmd(agent_cmd, model=model)
    env = _dispatch_env(thinking_cap)
    if sandbox_enabled(sandbox):
        cmd = _apply_sandbox(cmd, cwd=cwd, env=env)
    # Story 13.3-001: the agent runs under --dangerously-skip-permissions, so any
    # untrusted text woven into the prompt (story bodies, issue/PR comments) is a
    # prompt-injection surface. Sanitize the assembled prompt at this single
    # dispatch boundary — stripping zero-width/bidi Unicode, HTML comment/script,
    # and data:/base64 payloads — before it ever reaches the subprocess. Clean
    # prompts round-trip unchanged; suspicious payloads are logged (and flagged
    # for review above a threshold) rather than silently obeyed.
    prompt = sanitize_prompt(prompt, source=agent_type).cleaned
    if _is_streaming_cmd(cmd):
        return _dispatch_streaming(
            agent_type, prompt, cmd, timeout=timeout, stall_timeout=stall_timeout,
            transcript_path=transcript_path, on_progress=on_progress, env=env,
            cwd=cwd, parser=parser,
        )
    return _dispatch_captured(
        agent_type, prompt, cmd, timeout=timeout,
        transcript_path=transcript_path, env=env, cwd=cwd, parser=parser,
    )


def _dispatch_captured(
    agent_type: str,
    prompt: str,
    cmd: list[str],
    *,
    timeout: int,
    transcript_path: Path | None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    parser: str | None = None,
) -> AgentResult:
    """Buffered dispatch: run the agent and read all of stdout at once.

    Used for a non-streaming ``SDLC_AGENT_CMD`` (no ``stream-json``) and as the
    long-standing fallback. Unwraps a ``--output-format json`` envelope when one
    is present; otherwise treats stdout as the raw agent response. ``env`` is the
    subprocess environment (Story 14.2-002: carries a ``MAX_THINKING_TOKENS`` cap);
    ``None`` means inherit the parent environment, so the no-cap path is unchanged.
    ``cwd`` (Story 17.2-001) is the per-story worktree the agent runs in; ``None``
    inherits the parent's cwd (the shared-root path, unchanged).

    Story 13.4-001: ``start_new_session=True`` puts the agent in its own session /
    process group so it is isolated from the controller's group. On timeout
    ``subprocess.run`` reaps the direct child; full graceful process-group
    TERM→KILL escalation (so spawned children cannot survive) lives on the
    streaming path, which is the default real-agent dispatch.
    """
    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            start_new_session=True,
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
        parser=parser,
    )


def _emit_progress(on_progress: ProgressCallback | None, event: dict[str, Any]) -> None:
    """Hand one parsed stream event to the progress callback, swallowing errors.

    Progress recording is strictly best-effort (Story 11.1-002): a failing sink
    (e.g. a transient ledger write error) must never abort the agent stream, so
    any exception is contained here.
    """
    if on_progress is None:
        return
    try:
        on_progress(event)
    except Exception:  # noqa: BLE001 - progress is best-effort, never fatal
        pass


def _dispatch_streaming(
    agent_type: str,
    prompt: str,
    cmd: list[str],
    *,
    timeout: int,
    stall_timeout: float | None = DEFAULT_STALL_TIMEOUT_S,
    transcript_path: Path | None,
    on_progress: ProgressCallback | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    parser: str | None = None,
) -> AgentResult:
    """Streamed dispatch: consume stdout line-by-line, teeing each to the transcript.

    Reads the agent's ``stream-json`` output incrementally so the per-stage
    transcript reflects live activity (Story 11.1-001). The terminal ``result``
    event is captured and handed to the same interpretation logic the captured
    path uses, so usage extraction and schema validation are identical. If no
    ``result`` event arrives (the stream wasn't well-formed), interpretation
    falls back to parsing the accumulated stdout — graceful degradation rather
    than failing the run. ``env`` is the subprocess environment (Story 14.2-002:
    a ``MAX_THINKING_TOKENS`` cap); ``None`` inherits the parent, so the no-cap
    path is unchanged.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            # Story 13.4-001: lead a new session/process group so the controller
            # can signal the whole group (the agent + any tool subprocess it
            # spawned) on timeout/stall — no orphaned child survives the kill.
            start_new_session=True,
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
    #
    # ``read_done`` + ``state_lock`` close a race: once the read loop has drained
    # stdout on its own, a watchdog that fires microseconds later (e.g. a child
    # that completed right at the deadline) must NOT flag a timeout and discard a
    # valid result. The watchdog only acts while the read is still outstanding,
    # and the loop only claims success while no timeout has fired — the lock makes
    # exactly one of the two win, so a completed child is never a false timeout.
    #
    # Story 13.4-001: there are now two ways the run can be killed — the existing
    # wall-clock ``timeout`` and a *heartbeat dead-man* that fires after
    # ``stall_timeout`` seconds with no output. ``stalled`` distinguishes the two
    # so the raised error names the cause. Both terminate the whole **process
    # group** (graceful SIGTERM → grace → SIGKILL) via ``_terminate``, so a
    # spawned tool subprocess cannot outlive the kill.
    timed_out = threading.Event()
    stalled = threading.Event()
    read_done = threading.Event()
    state_lock = threading.Lock()
    last_activity = [time.monotonic()]  # bumped on each stream line (heartbeat)

    def _terminate(*, reason_stalled: bool) -> None:
        # Claim the kill only while the read is still outstanding; a completed
        # read (read_done) means the child finished in time, so do nothing.
        with state_lock:
            if read_done.is_set():
                return
            timed_out.set()
            if reason_stalled:
                stalled.set()
        # Graceful group kill unblocks the still-outstanding stdout read.
        _terminate_process_group(proc)

    def _on_timeout() -> None:
        _terminate(reason_stalled=False)

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    # Heartbeat dead-man: an output-idle monitor that kills the agent if no
    # stream line has arrived for ``stall_timeout`` seconds — bounding a hang far
    # inside the (much larger) wall-clock ``timeout``. Disabled when
    # ``stall_timeout`` is falsy, so the wall-clock guard is then the only bound.
    heartbeat: threading.Thread | None = None
    if stall_timeout and stall_timeout > 0:
        poll = max(0.01, min(stall_timeout / 2, _HEARTBEAT_POLL_S))

        def _heartbeat_monitor() -> None:
            while not read_done.wait(timeout=poll):
                if timed_out.is_set():
                    return
                if time.monotonic() - last_activity[0] >= stall_timeout:
                    _terminate(reason_stalled=True)
                    return

        heartbeat = threading.Thread(target=_heartbeat_monitor, daemon=True)
        heartbeat.start()

    transcript = _StreamTranscript(transcript_path)
    raw_lines: list[str] = []
    result_event: dict[str, Any] | None = None
    # Issue #120: a structured 429 surfaces its absolute window-reset epoch on a
    # separate ``rate_limit_event`` stream line (``rate_limit_info.resetsAt``),
    # not in the terminal result envelope. Capture it as it passes so the
    # structured-429 fallback in _interpret() can resume precisely on reset
    # instead of falling back to the full rolling-window heuristic.
    stream_resets_at: float | None = None
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
                last_activity[0] = time.monotonic()  # heartbeat: output seen
                transcript.append(line)
                raw_lines.append(line)
                event = _parse_stream_line(line)
                if event is not None:
                    _emit_progress(on_progress, event)
                    if event.get("type") == "result":
                        result_event = event
                    elif event.get("type") == "rate_limit_event":
                        captured = _extract_resets_at(event)
                        if captured is not None:
                            stream_resets_at = captured
        # Claim completion: if the watchdog hasn't already fired, mark the read
        # done so a later firing is a no-op. If it *has* fired, the kill is what
        # ended the read — honour the timeout.
        with state_lock:
            if not timed_out.is_set():
                read_done.set()
        if timed_out.is_set():
            try:
                proc.wait(timeout=_POST_STREAM_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            watchdog.cancel()
            drainer.join(timeout=1)
            # Story 13.4-001: name the cause (stall vs wall clock), then quarantine
            # the partial transcript so the kill can be reviewed and the path is
            # surfaced in the error → the controller records it in the ledger.
            if stalled.is_set():
                reason = f"stalled: no output for {stall_timeout:g}s"
            else:
                reason = f"timed out after {timeout}s"
            transcript.append(f"\n--- KILLED ({reason}) ---\n")
            transcript.close()
            quarantined = _quarantine_transcript(transcript_path, reason)
            detail = (
                f"; transcript quarantined at {quarantined}" if quarantined else ""
            )
            raise AgentDispatchError(f"{agent_type} agent {reason}{detail}")
        # stdout hit EOF, so the child is exiting; bound the reap so a process
        # that closes stdout but lingers cannot hang the run (the watchdog is a
        # no-op now that read_done is set). A lingering child is group-killed so
        # any orphaned tool subprocess goes with it (Story 13.4-001).
        try:
            returncode = proc.wait(timeout=_POST_STREAM_WAIT_S)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            returncode = proc.wait()
    finally:
        watchdog.cancel()
        if heartbeat is not None:
            heartbeat.join(timeout=1)

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
        stream_resets_at=stream_resets_at,
        parser=parser,
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
    stream_resets_at: float | None = None,
    parser: str | None = None,
) -> AgentResult:
    """Hand the collected output to the harness's output parser (Story 20.1-002).

    Collection — running the subprocess, streaming vs captured, the kill-switch —
    is harness-neutral and stays here; *interpretation* — envelope shape,
    usage/cost extraction, rate-limit and context-overflow recognition — is
    per-harness and owned by an :class:`~sdlc.parsers.OutputParser` resolved by id.
    ``parser`` is the harness's declared parser id; ``None`` selects the built-in
    Claude parser, so the default path is byte-for-byte today's. The Claude-specific
    logic that used to live inline here now lives in
    :class:`~sdlc.parsers.ClaudeStreamJsonParser`, preserved verbatim.

    ``envelope`` is the terminal ``result`` event (streaming) or None — derived
    from ``stdout`` by the Claude parser when None. ``stream_resets_at`` (issue
    #120) is the absolute rate-limit reset epoch captured from a stream line, used
    by the Claude parser to resume precisely on reset.

    The parsers module is imported **lazily** here so the dispatch↔parsers
    dependency stays one-way: parsers imports dispatch's errors / ``AgentResult``
    at module load, while dispatch imports parsers only at call time.
    """
    from sdlc.parsers import CollectedOutput, get_parser

    return get_parser(parser).parse(
        CollectedOutput(
            agent_type=agent_type,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            transcript_path=transcript_path,
            envelope=envelope,
            streaming=streaming,
            stream_resets_at=stream_resets_at,
        )
    )
