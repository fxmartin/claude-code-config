# ABOUTME: Harness capability probe + preflight decision (Story 20.5-001).
# ABOUTME: Resolves what a harness can do, optionally probes its CLI, and gates the run mode safely.

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from sdlc.harness import HarnessConfig

# Canonical capability flags the controller gates run modes on. Resolution fills
# any undeclared key as ``False`` — an undeclared capability is assumed ABSENT,
# so a harness only earns a capability it explicitly claims (conservative
# default). A registry entry may also declare extra, non-canonical flags; those
# are preserved verbatim.
CAPABILITY_KEYS: tuple[str, ...] = (
    "worktree_isolation",
    "parallel",
    "json_contract",
    "usage_tracking",
    "rate_limit_aware",
)

# Run modes the executor recognises — Epic-17's `mode` authority
# (`authoritative_mode` in build.py) is the source of the *requested* mode; this
# module only decides whether the harness can honour it.
MODE_SERIAL = "serial"
MODE_PARALLEL = "parallel"

# How long a probe command may run before it is treated as unavailable. A probe
# is a cheap "is the CLI installed/authenticated?" check, so this is short.
_PROBE_TIMEOUT_SECONDS = 10

# A probe runner takes the probe argv and returns ``(returncode, detail)``.
# Injected by tests; the default shells out via :func:`_default_probe_runner`.
ProbeRunner = Callable[[list[str]], "tuple[int, str]"]


class ProbeStatus(str, Enum):
    """Outcome of an optional harness probe command."""

    AVAILABLE = "available"  # probe ran and succeeded (CLI installed/authenticated)
    UNAVAILABLE = "unavailable"  # probe ran and failed (missing CLI / not authed)
    UNKNOWN = "unknown"  # no probe command declared — not probed


@dataclass(frozen=True)
class ProbeResult:
    """The result of probing a harness CLI for availability."""

    status: ProbeStatus
    command: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class HarnessPreflight:
    """The resolved capabilities and safe run mode for one harness.

    ``effective_mode`` is the mode the controller should actually run in: it
    equals ``requested_mode`` unless a capability gap forced a downgrade (then it
    is the safe alternative, e.g. ``serial``). ``warnings`` explains every
    downgrade or probe failure so nothing degrades silently.
    """

    harness: str
    capabilities: dict[str, bool]
    requested_mode: str
    effective_mode: str
    probe: ProbeResult
    warnings: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.effective_mode != self.requested_mode

    def log_lines(self) -> list[str]:
        """Human-readable preflight lines for stderr / the ledger event log."""
        summary = " ".join(
            f"{key}={'yes' if value else 'no'}"
            for key, value in self.capabilities.items()
        )
        lines = [f"harness {self.harness!r}: capabilities {summary}"]
        if self.probe.status is not ProbeStatus.UNKNOWN:
            lines.append(f"harness {self.harness!r}: probe {self.probe.status.value}")
        lines.append(
            f"harness {self.harness!r}: mode={self.effective_mode}"
            + (f" (requested {self.requested_mode})" if self.degraded else "")
        )
        lines.extend(self.warnings)
        return lines


def resolve_capabilities(harness: HarnessConfig) -> dict[str, bool]:
    """Resolve a harness's full capability map.

    Every canonical key in :data:`CAPABILITY_KEYS` is present in the result:
    declared flags win, undeclared canonical keys default to ``False``. Any
    extra (non-canonical) declared flags are preserved.
    """
    resolved: dict[str, bool] = dict.fromkeys(CAPABILITY_KEYS, False)
    for key, value in harness.capabilities.items():
        resolved[key] = bool(value)
    return resolved


def _default_probe_runner(argv: list[str]) -> tuple[int, str]:
    """Run a probe command, returning ``(returncode, detail)`` (best effort)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return 127, f"command not found: {argv[0] if argv else ''}"
    except subprocess.TimeoutExpired:
        return 124, "probe command timed out"
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode, detail


def probe_harness(
    harness: HarnessConfig,
    *,
    runner: ProbeRunner | None = None,
) -> ProbeResult:
    """Confirm a harness CLI is installed/authenticated via its probe command.

    When the harness declares no ``probe`` command the result is
    :attr:`ProbeStatus.UNKNOWN` and no subprocess runs. Otherwise the probe argv
    is run; a zero exit is :attr:`ProbeStatus.AVAILABLE`, anything else is
    :attr:`ProbeStatus.UNAVAILABLE` with the captured detail.
    """
    command = harness.probe
    if not command:
        return ProbeResult(status=ProbeStatus.UNKNOWN)
    run = runner or _default_probe_runner
    returncode, detail = run(shlex.split(command))
    status = ProbeStatus.AVAILABLE if returncode == 0 else ProbeStatus.UNAVAILABLE
    return ProbeResult(status=status, command=command, detail=detail)


def preflight_harness(
    harness: HarnessConfig,
    *,
    requested_mode: str = MODE_SERIAL,
    probe_runner: ProbeRunner | None = None,
) -> HarnessPreflight:
    """Resolve capabilities and decide a safe run mode for ``harness``.

    Story 20.5-001 AC2: when the requested mode exceeds what the harness can do
    (e.g. ``parallel`` without worktree isolation), the controller selects a safe
    alternative (serial) and records a warning, rather than failing mid-run. A
    declared probe command is run and an ``unavailable`` result surfaces as a
    warning too. Serial is always supportable, so it never degrades.

    The mode decision is delegated to :func:`sdlc.degradation.evaluate_degradations`
    (Story 20.5-002), the single source of truth for the degradation matrix.
    Preflight surfaces only the *mode* fallback here; the usage / rate-limit
    fallbacks in the same plan are recorded by the build flow, not as preflight
    warnings. The import is local to avoid a module-load import cycle (the
    degradation module imports this module's mode constants).
    """
    from sdlc.degradation import DegradationKind, evaluate_degradations

    capabilities = resolve_capabilities(harness)
    probe = probe_harness(harness, runner=probe_runner)
    warnings: list[str] = []

    plan = evaluate_degradations(
        harness.name, capabilities, requested_mode=requested_mode
    )
    effective_mode = plan.effective_mode
    for degradation in plan.degradations:
        if degradation.kind is DegradationKind.PARALLEL_TO_SERIAL:
            warnings.append(degradation.message)

    if probe.status is ProbeStatus.UNAVAILABLE:
        warnings.append(
            f"harness {harness.name!r} probe failed: "
            f"{probe.detail or 'CLI unavailable'}"
        )

    return HarnessPreflight(
        harness=harness.name,
        capabilities=capabilities,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        probe=probe,
        warnings=warnings,
    )
