# ABOUTME: What a fleet worker advertises to the queue service — host, pools, harnesses,
# ABOUTME: sandbox and the clones under ~/Work. Story 35.2-001; registered by `queue run --worker`.

from __future__ import annotations

import shlex
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from sdlc.registry import normalize_dashboard_url

if TYPE_CHECKING:
    from sdlc.queue import QueueBackend, WorkerRecord

__all__ = ["WorkerProfile", "default_work_dir", "detect_worker_profile"]


def default_work_dir() -> Path:
    """Where this fleet keeps its clones: ``~/Work``."""
    return Path.home() / "Work"


@dataclass(frozen=True)
class WorkerProfile:
    """One worker's identity and capabilities, as it registers them.

    Everything but ``slots``/``slots_free`` is detected once at start-up; the two
    slot figures change with load, so they are supplied per heartbeat.
    """

    name: str
    host: str
    pools: list[str] = field(default_factory=list)
    harnesses: list[str] = field(default_factory=list)
    sandbox: str | None = None
    repos: list[str] = field(default_factory=list)
    # Where this worker's `sdlc dashboard --host <tailnet-ip>` answers (Story 35.4-002);
    # stamped on each run it pushes so the XPS can read the run's transcripts.
    dashboard_url: str | None = None

    def register_with(
        self, queue: "QueueBackend", *, slots: int, slots_free: int, **extra: Any
    ) -> "WorkerRecord":
        """Register on ``queue``; calling it again is the heartbeat.

        ``extra`` passes through to the backend's ``register_worker`` — the local
        store takes a ``now`` so a scheduler on a fake clock stays coherent.
        """
        return queue.register_worker(
            self.name,
            host=self.host,
            pools=self.pools,
            harnesses=self.harnesses,
            sandbox=self.sandbox,
            repos=self.repos,
            slots=slots,
            slots_free=slots_free,
            **extra,
        )


def _clones(work_dir: Path) -> list[str]:
    """Names of the git clones directly under ``work_dir`` (a ``.git`` dir *or* file)."""
    try:
        entries = sorted(work_dir.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.name.startswith(".") and entry.is_dir() and (entry / ".git").exists()
    ]


def _harness_binaries() -> dict[str, str]:
    """``{harness name: the CLI that proves it is installed}`` from the bundled registry.

    The registry's own ``probe`` command (``codex --version``) is the better
    witness when it has one; otherwise the first word of the harness command.
    A registry that cannot be read leaves just the built-in ``claude``.
    """
    from sdlc.harness import HarnessError, load_harnesses_config
    from sdlc.role_routing import default_registry_path

    path = default_registry_path()
    registry = {}
    if path is not None:
        try:
            registry = load_harnesses_config(path)
        except (HarnessError, OSError, ValueError):
            registry = {}
    binaries: dict[str, str] = {}
    for name, config in registry.items():
        if not config.enabled:
            continue
        words = shlex.split(config.probe or config.command)
        if words:
            binaries[name] = words[0]
    return binaries or {"claude": "claude"}


def detect_worker_profile(
    name: str,
    *,
    pools: Iterable[str] = (),
    host: str | None = None,
    work_dir: Path | None = None,
    probe: Callable[[str], bool] | None = None,
    runtime: Callable[[], str] | None = None,
    dashboard_url: str | None = None,
) -> WorkerProfile:
    """Probe this machine for what it can run.

    ``probe`` is doctor's dependency probe (a tool on PATH that answers
    ``--version``) and ``runtime`` the sandbox's container-runtime detection —
    both injectable so a test never depends on the machine it runs on. Pools are
    declared, not detected: they name a subscription, and only the operator
    knows which one this machine is logged into.
    """
    from sdlc.dispatch import SandboxUnavailableError, detect_container_runtime
    from sdlc.doctor import _default_dep_probe

    name = name.strip()
    if not name:
        raise ValueError("worker name must not be blank")
    declared = [pool.strip() for pool in pools]
    if any(not pool for pool in declared):
        raise ValueError("a pool name must not be blank")

    origin = normalize_dashboard_url(dashboard_url) if dashboard_url is not None else None

    check = probe or _default_dep_probe
    harnesses = [harness for harness, binary in _harness_binaries().items() if check(binary)]
    try:
        sandbox: str | None = (runtime or detect_container_runtime)()
    except SandboxUnavailableError:
        sandbox = None
    return WorkerProfile(
        name=name,
        host=host or socket.gethostname().split(".")[0],
        pools=list(dict.fromkeys(declared)),
        harnesses=harnesses,
        sandbox=sandbox,
        repos=_clones(work_dir if work_dir is not None else default_work_dir()),
        dashboard_url=origin,
    )
