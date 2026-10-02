# ABOUTME: What a fleet worker advertises to the queue service — host, pools, harnesses,
# ABOUTME: sandbox and the clones under ~/Work. Story 35.2-001; registered by `queue run --worker`.
# ABOUTME: Also the repo auto-sync that runs before dispatch (Story 35.2-002).

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.parse import urlparse

if TYPE_CHECKING:
    from sdlc.queue import JobRecord, QueueBackend, WorkerRecord

__all__ = [
    "PreparedRepo",
    "RepoPreparer",
    "RepoRefused",
    "WorkerProfile",
    "default_work_dir",
    "detect_worker_profile",
    "prepare_repo",
    "repo_origin",
    "same_origin",
]


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
    )


# ---------------------------------------------------------------------------
# Repo auto-sync before dispatch (Story 35.2-002)
#
# A worker may not have touched a repo in days, or ever. Before a claimed job is
# launched the worker brings its clone to the forge's ``main`` — cloning it if it
# is absent — so a build never starts from a stale or missing tree. The enqueuer
# records the clone's ``origin`` in the job's ``requirements`` so the job is
# self-describing: the worker clones from, and checks against, what the job says
# rather than guessing from a path that is only meaningful on the enqueuing host.
# ---------------------------------------------------------------------------

# Network-bound git calls (fetch, clone) get the push ceiling; local plumbing is
# quick. ``GIT_TERMINAL_PROMPT=0`` makes a missing credential fail instead of
# hanging a headless worker on a username prompt.
_GIT_TIMEOUT_SECONDS = 120
_BASE_BRANCH = "main"


class RepoRefused(Exception):
    """The worker will not run this job against its clone.

    ``retryable`` separates the one refusal a human fixes by tidying their own
    tree (a dirty checkout — the job goes back to ``queued``) from the ones that
    need an operator decision (wrong origin, diverged ``main``, no clone — the
    job is parked ``blocked``).
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class PreparedRepo:
    """Where the job runs, and the sha its tree was brought to (``None`` = not synced)."""

    path: Path
    sha: str | None


RepoPreparer = Callable[["JobRecord"], PreparedRepo]

_SCP_URL = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def _origin_key(url: str) -> str:
    """``host/path`` for a remote URL, whatever its scheme, user or ``.git`` suffix.

    The same repo is spelled ``git@host:o/r.git``, ``ssh://git@host/o/r`` and
    ``https://host/o/r`` on different machines, so identity is host plus path. A
    local path (a bare repo on disk) has no host and compares as itself.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        host, path = (parsed.hostname or "").lower(), parsed.path
    elif (scp := _SCP_URL.match(url)) and not url.startswith(("/", ".")):
        host, path = scp["host"].lower(), scp["path"]
    else:
        host, path = "", url
    path = path.rstrip("/").removesuffix(".git").rstrip("/")
    return f"{host}/{path.lstrip('/')}" if host else path


def same_origin(left: str, right: str) -> bool:
    """Whether two remote URLs name the same repository."""
    return _origin_key(left) == _origin_key(right)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def repo_origin(root: Path) -> str | None:
    """``root``'s ``origin`` URL (``git remote get-url origin``), ``None`` when it has none.

    What an enqueue records so the job names the forge it belongs to. Offline and
    instant: it reads local config only.
    """
    try:
        res = _git(root, "remote", "get-url", "origin")
    except (OSError, subprocess.SubprocessError):
        return None
    url = res.stdout.strip()
    return url if res.returncode == 0 and url else None


def origin_requirements(root: Path) -> str | None:
    """The ``requirements`` JSON an enqueue records for ``root`` (``None`` without an origin)."""
    origin = repo_origin(root)
    return json.dumps({"origin": origin}) if origin else None


def _recorded_origin(job: "JobRecord") -> str | None:
    if not job.requirements:
        return None
    try:
        data = json.loads(job.requirements)
    except ValueError:
        return None
    value = data.get("origin") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """:func:`_git`, with a timeout or missing git surfaced as a refusal."""
    try:
        return _git(root, *args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoRefused(f"git {args[0]} failed in {root}: {exc}") from exc


def _dirty_refusal(root: Path, paths: list[str]) -> RepoRefused:
    from sdlc.build import _DIRTY_TREE_MAX_LISTED

    listed = ", ".join(paths[:_DIRTY_TREE_MAX_LISTED])
    more = len(paths) - _DIRTY_TREE_MAX_LISTED
    if more > 0:
        listed += f" (+{more} more)"
    return RepoRefused(
        f"DIRTY_WORKING_TREE: {root} has {len(paths)} uncommitted tracked change(s): "
        f"{listed}. The worker never stashes (issue #590) — commit or stash them "
        "yourself; the job stays queued and is retried.",
        retryable=True,
    )


def _clone(origin: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Plain `git clone`: the worker's own `gh`/`glab` credentials reach git
        # through the credential helpers `gh auth setup-git` / `glab auth
        # git-credential` install, so no token is handled here.
        res = subprocess.run(
            ["git", "clone", "-q", origin, str(target)],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoRefused(f"could not clone {origin} into {target}: {exc}") from exc
    if res.returncode != 0:
        raise RepoRefused(
            f"could not clone {origin} into {target}: {res.stderr.strip() or 'git clone failed'}"
        )


def prepare_repo(job: "JobRecord", *, work_dir: Path | None = None) -> PreparedRepo:
    """Bring the job's clone to the forge's ``main`` before it is dispatched.

    ``git fetch origin && git checkout -q main && git merge --ff-only
    origin/main``, then the resulting sha. An absent clone is made from the
    origin the job recorded at enqueue — at the job's own path when this machine
    has that directory's parent, otherwise under ``work_dir`` (``~/Work``): a
    path recorded on another machine means nothing here.

    Refuses (:class:`RepoRefused`) rather than repairing: a tracked-dirty tree
    (never stashed — the #590 rule), an ``origin`` that is not the one the job
    records, a ``main`` that cannot fast-forward, a directory that is no clone.
    A job that recorded no origin predates this sync (or came from a repo with no
    remote) and is returned untouched.
    """
    from sdlc.build import dirty_tree_paths

    recorded = _recorded_origin(job)
    declared = Path(job.repo)
    if recorded is None:
        return PreparedRepo(path=declared, sha=None)

    root = work_dir if work_dir is not None else default_work_dir()
    target = declared if declared.exists() else root / declared.name
    if not target.exists():
        _clone(recorded, target)
    elif not (target / ".git").exists():
        raise RepoRefused(f"{target} exists but is not a git clone")

    actual = repo_origin(target)
    if actual is None or not same_origin(actual, recorded):
        raise RepoRefused(
            f"origin mismatch: {target} has origin {actual or '(none)'} "
            f"but the job records {recorded}"
        )
    if paths := dirty_tree_paths(target):
        raise _dirty_refusal(target, paths)

    for args in (("fetch", "origin"), ("checkout", "-q", _BASE_BRANCH),
                 ("merge", "--ff-only", f"origin/{_BASE_BRANCH}")):
        res = _run_git(target, *args)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip()
            hint = " (local main has diverged from origin/main)" if args[0] == "merge" else ""
            step = "fast-forward to origin/main" if args[0] == "merge" else " ".join(args)
            raise RepoRefused(f"could not {step} in {target}{hint}: {detail}")
    head = _run_git(target, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise RepoRefused(f"could not read HEAD of {target} after syncing")
    return PreparedRepo(path=target, sha=head.stdout.strip())
