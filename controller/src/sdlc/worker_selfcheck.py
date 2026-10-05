# ABOUTME: A fleet worker proves it can run an agent before it registers (Story 35.2-007).
# ABOUTME: A TCC path verdict plus one real haiku turn, both from the worker's own launchd context.

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from sdlc.model_probe import OK, ProbeRunner, classify_probe, default_state_dir
from sdlc.model_routing import HAIKU, TIER_MODEL_IDS
from sdlc.rate_limit import detect_rate_limit

__all__ = [
    "SELF_CHECK_CAP_SECONDS",
    "SELF_CHECK_RETRY_SECONDS",
    "STATE_FILENAME",
    "SelfCheckResult",
    "group_runner",
    "read_last_result",
    "run_self_check",
    "running_under_launchd",
    "tcc_verdict",
]

# Why these numbers: the probe is the 34.1-002 entitlement turn, and 90 s is the
# stall cap `dispatch._dispatch_streaming`'s dead-man works to. A dialog nobody
# has answered never resolves by itself, so the retry only needs to notice the
# moment someone does.
SELF_CHECK_CAP_SECONDS = 90
SELF_CHECK_RETRY_SECONDS = 60
STATE_FILENAME = "worker-self-check.json"

# Directly under $HOME, macOS gates these behind a Privacy & Security prompt that
# a LaunchAgent can neither show nor answer. Cloud-drive roots are protected too.
_TCC_ROOTS = (
    "Documents",
    "Desktop",
    "Downloads",
    "Library/Mobile Documents",  # iCloud Drive
    "Library/CloudStorage",  # Dropbox, Google Drive, OneDrive (File Provider)
)
# ...and the legacy cloud-drive folders in $HOME, named with a suffix per account.
_TCC_HOME_PREFIXES = ("Dropbox", "Google Drive", "OneDrive", "iCloud Drive")

# What the claude CLI reads from ~/.claude at start-up, on top of the directory.
_CLAUDE_ENTRIES = ("settings.json", "CLAUDE.md", "hooks", "commands", "agents", "skills")

_TCC_FIX = (
    "launchd agents cannot read TCC protected folders; "
    "move the checkout (nix-install: ~/.config/nix-install)"
)
_STALL = (
    "agent produced no output in {cap}s — a dialog is probably waiting on this Mac "
    "(Keychain / Privacy & Security) or ~/.claude resolves into a protected folder"
)


def _protected_roots(home: Path) -> list[Path]:
    roots = [home / root for root in _TCC_ROOTS]
    try:
        siblings = sorted(home.iterdir())
    except OSError:
        siblings = []
    roots += [p for p in siblings if p.name.startswith(_TCC_HOME_PREFIXES)]
    return roots


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def tcc_verdict(
    *, claude_dir: Path | None = None, home: Path | None = None, system: str | None = None
) -> str | None:
    """Name the first ``~/.claude`` entry that resolves into a TCC-protected folder.

    A path test on ``os.path.realpath`` of ``~/.claude`` and of each entry the CLI
    reads, against the protected roots: cheap and deterministic. Over ssh these
    paths read fine (sshd carries disk access and sees no dialogs), which is why
    the worker must ask from its own launchd context — and why a launchd agent
    that cannot read them stalls every agent for 300 s with no signal. ``None``
    when nothing is protected, and always off macOS, where there is no TCC.
    """
    if (system or platform.system()) != "Darwin":
        return None
    home = home or Path.home()
    claude_dir = claude_dir or home / ".claude"
    roots = [(root, os.path.realpath(root)) for root in _protected_roots(home)]
    candidates = [("~/.claude", claude_dir)] + [
        (f"~/.claude/{name}", claude_dir / name) for name in _CLAUDE_ENTRIES
    ]
    for label, path in candidates:
        resolved = os.path.realpath(path)
        for root, real_root in roots:
            if _inside(resolved, real_root):
                shown = "~/" + root.relative_to(home).as_posix()
                return f"{label} → {shown}/…: {_TCC_FIX}"
    return None


def group_runner(argv: list[str], timeout_s: float = SELF_CHECK_CAP_SECONDS) -> tuple[int, str]:
    """A :data:`~sdlc.model_probe.ProbeRunner` that kills the probe's whole process group.

    ``subprocess.run(timeout=)`` kills only the child, and an agent stalled on a
    dialog may have children of its own. Own session, then SIGTERM → SIGKILL on
    the group, so nothing is left holding the dialog open. Exit 124 is a stall.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, f"command not found: {argv[0] if argv else ''}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return 124, "probe command timed out"
    except BaseException:  # Ctrl-C / SIGTERM mid-probe must not orphan the agent
        _kill_group(proc)
        raise
    return proc.returncode, (stderr or stdout or "").strip()


def _kill_group(proc: "subprocess.Popen[str]") -> None:
    # SIGKILL follows SIGTERM even when the leader left: its children may not have.
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # gone — macOS answers EPERM for a group whose leader is a zombie
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def running_under_launchd(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process is a launchd job — not a login shell, an IDE or ssh.

    launchd sets ``XPC_SERVICE_NAME`` to the job's label; a terminal's is ``0``
    and an app-launched shell's is ``application.<bundle id>.<n>``.
    """
    name = (environ if environ is not None else os.environ).get("XPC_SERVICE_NAME", "")
    return name not in ("", "0") and not name.startswith("application.")


@dataclass(frozen=True)
class SelfCheckResult:
    """One self-check: did an agent turn complete from this worker's own context."""

    ok: bool
    at: str
    reason: str | None = None
    tcc: str | None = None
    launchd: bool = False

    def to_heartbeat(self) -> dict[str, object]:
        """The ``self_check: {ok, at, reason}`` a registration carries."""
        return {"ok": self.ok, "at": self.at, "reason": self.reason}


def _first_line(text: str, limit: int = 200) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "no output")
    return line[:limit]


def _probe(runner: ProbeRunner | None, cap: int) -> str | None:
    """Run the 34.1-002 entitlement probe; ``None`` when a turn completed, else why not."""
    argv = ["claude", "-p", "ok", "--model", TIER_MODEL_IDS[HAIKU], "--output-format", "json"]
    try:
        code, detail = (runner or group_runner)(argv, timeout_s=cap)
    except Exception as exc:  # noqa: BLE001 - a broken probe is a failed check, not a crash
        return f"agent probe could not run: {exc}"
    if code == 124:
        return _STALL.format(cap=cap)
    if code == 127:
        return "claude CLI not found on this worker's PATH"
    if classify_probe(code, detail) == OK:
        return None
    # A rate limit means a turn ran and was refused: the queue's pause owns that.
    if detect_rate_limit(detail) is not None:
        return None
    return f"agent probe failed (exit {code}): {_first_line(detail)}"


def run_self_check(
    *,
    runner: ProbeRunner | None = None,
    cap: int = SELF_CHECK_CAP_SECONDS,
    claude_dir: Path | None = None,
    home: Path | None = None,
    system: str | None = None,
    clock: Callable[[], datetime] | None = None,
    state_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> SelfCheckResult:
    """The TCC path verdict, then — only if it is clear — one real agent turn.

    Run in the worker's own process, so it sees what launchd lets the agent see.
    The outcome is recorded under ``state_dir`` for `sdlc doctor`, which runs in
    a shell and cannot witness launchd's context itself.
    """
    at = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    tcc = tcc_verdict(claude_dir=claude_dir, home=home, system=system)
    reason = tcc if tcc is not None else _probe(runner, cap)
    result = SelfCheckResult(
        ok=reason is None,
        at=at,
        reason=reason,
        tcc=tcc,
        launchd=running_under_launchd(environ),
    )
    _record(result, state_dir or default_state_dir())
    return result


def _record(result: SelfCheckResult, state_dir: Path) -> None:
    payload = {
        "ok": result.ok,
        "at": result.at,
        "reason": result.reason,
        "tcc": result.tcc,
        "launchd": result.launchd,
    }
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / STATE_FILENAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass  # doctor then reports "no self-check recorded"; the worker still gates on the result


def read_last_result(state_dir: Path | None = None) -> dict[str, object] | None:
    """The last recorded self-check, or ``None`` when absent or unreadable."""
    try:
        data = json.loads(((state_dir or default_state_dir()) / STATE_FILENAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return None
    return data
