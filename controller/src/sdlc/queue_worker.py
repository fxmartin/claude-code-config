# ABOUTME: What a fleet worker advertises to the queue service — host, pools, harnesses,
# ABOUTME: sandbox and the clones under ~/Work. Story 35.2-001; registered by `queue run --worker`.
# ABOUTME: Also the repo auto-sync that runs before dispatch (Story 35.2-002).

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Protocol
from urllib.parse import urlparse, urlsplit, urlunsplit

from sdlc.registry import normalize_dashboard_url

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


# ---------------------------------------------------------------------------
# Repo auto-sync before dispatch (Story 35.2-002)
#
# A worker may not have touched a repo in days, or ever. Before a claimed job is
# launched the worker brings its clone to the forge's default branch (``main``)
# — cloning it if it is absent — so a build never starts from a stale or missing
# tree. The enqueuer records the clone's ``origin`` in the job's ``requirements``
# so the job is self-describing: the worker clones from, and checks against, what
# the job says rather than guessing from a path only the enqueuing host knows.
# ---------------------------------------------------------------------------

# Network-bound git calls (fetch, clone) get the push ceiling; local plumbing is
# quick. ``GIT_TERMINAL_PROMPT=0`` makes a missing credential fail instead of
# hanging a headless worker on a username prompt.
_GIT_TIMEOUT_SECONDS = 120

# An ssh URL needs its user: ``git@`` is the forge's account name, not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


class RepoRefused(Exception):
    """The worker will not run this job against its clone.

    ``retryable`` separates the refusals that clear without an operator — a
    dirty checkout its owner tidies, a clone another live job is using, a forge
    that is slow or unreachable (a fetch or clone that failed or timed out);
    the job goes back to ``queued`` — from the ones that need an operator
    decision (wrong origin, a default branch that cannot check out or
    fast-forward, a directory that is no clone — the job is parked
    ``blocked``).
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


class RepoPreparer(Protocol):
    """The shape of :func:`prepare_repo`, the scheduler's injectable sync seam."""

    def __call__(
        self, job: "JobRecord", *, busy_repos: Collection[str] = ...
    ) -> PreparedRepo: ...

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


def _without_credentials(url: str) -> str:
    """``url`` with no secret in it: what a job may record and a reason may print.

    A job is stored, served to every queue client and printed by `queue list`,
    yet an ``origin`` can embed a token (``https://oauth2:<token>@host/…``, or a
    GitHub token alone in the user slot). An http(s) URL loses its whole
    userinfo — the worker's credential helper supplies its own; an ssh URL keeps
    its user and loses only a password. An scp-style ``git@host:path`` or a
    local path cannot carry a secret and comes back as it is. Identity is
    unchanged: :func:`same_origin` already ignores the userinfo.
    """
    parts = urlsplit(url)
    if not (parts.scheme and parts.netloc) or "@" not in parts.netloc:
        return url
    userinfo, _, host = parts.netloc.rpartition("@")
    user = userinfo.partition(":")[0]
    keep = f"{user}@" if parts.scheme in _SSH_SCHEMES and user else ""
    return urlunsplit(parts._replace(netloc=keep + host))


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
    """The ``requirements`` JSON an enqueue records for ``root`` (``None`` without an origin).

    The origin is recorded without credentials (:func:`_without_credentials`):
    a job never carries a token, and a worker clones with its own.
    """
    origin = repo_origin(root)
    return json.dumps({"origin": _without_credentials(origin)}) if origin else None


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
    """:func:`_git`, with a timeout (retryable) or missing git surfaced as a refusal."""
    try:
        return _git(root, *args)
    except subprocess.TimeoutExpired as exc:
        raise RepoRefused(f"git {args[0]} timed out in {root}: {exc}", retryable=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoRefused(f"git {args[0]} failed in {root}: {exc}") from exc


def _default_branch(root: Path) -> str:
    """The forge's default branch: the one ``origin/HEAD`` names, else ``main``.

    The ref the build cuts story branches from (Story 23.2-001), so a repo whose
    default is not ``main`` syncs instead of parking every job ``blocked``.
    """
    from sdlc.build import _origin_default_ref

    return _origin_default_ref(root).removeprefix("origin/")


def _diverged_hint(root: Path, branch: str) -> str:
    """Name the divergence only when there is one: a merge also aborts on local changes."""
    try:
        res = _git(root, "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}")
    except (OSError, subprocess.SubprocessError):
        return ""
    # Exit 1 is git's "not an ancestor"; anything else is no verdict at all.
    diverged = f" (local {branch} has diverged from origin/{branch})"
    return diverged if res.returncode == 1 else ""


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
    shown = _without_credentials(origin)
    # The origin comes from a job body; one shaped like an option
    # (`--upload-pack=…`) must never reach git's argument parser.
    if origin.startswith("-"):
        raise RepoRefused(f"refusing to clone {shown!r}: an origin that reads as a git option")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Plain `git clone`: the worker's own `gh`/`glab` credentials reach git
        # through the credential helpers `gh auth setup-git` / `glab auth
        # git-credential` install, so no token is handled here.
        res = subprocess.run(
            ["git", "clone", "-q", "--", origin, str(target)],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        # The timeout SIGKILLs git, so it cannot remove its half-written clone —
        # which the retry would otherwise find and judge as a (dirty) clone.
        shutil.rmtree(target, ignore_errors=True)
        # Not `exc`: its text is the argv, origin and all.
        raise RepoRefused(
            f"could not clone {shown} into {target}: "
            f"timed out after {_GIT_TIMEOUT_SECONDS}s",
            retryable=True,
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoRefused(f"could not clone {shown} into {target}: {exc}") from exc
    if res.returncode != 0:
        # The forge, not the job: down, rebooting, a credential to renew. That
        # clears without anyone touching the job, so it goes back to be retried.
        raise RepoRefused(
            f"could not clone {shown} into {target}: "
            f"{res.stderr.strip() or 'git clone failed'}",
            retryable=True,
        )


def prepare_repo(
    job: "JobRecord", *, work_dir: Path | None = None, busy_repos: Collection[str] = ()
) -> PreparedRepo:
    """Bring the job's clone to the forge's default branch before it is dispatched.

    ``git fetch origin && git checkout -q main && git merge --ff-only
    origin/main``, then the resulting sha — with the branch ``origin/HEAD``
    names in place of ``main`` when the forge's default is another. The clone
    is the job's own path when that exists on this machine, otherwise
    ``work_dir/<name>`` (``~/Work``): a path recorded on another machine means
    nothing here. An absent clone is made from the origin the job recorded at
    enqueue.

    ``busy_repos`` are the clones other live jobs are using. The sync checks out
    and fast-forwards the clone itself — a write to the shared checkout, which
    the claim's build/build overlap (Story 32.1-003) assumes no job makes
    mid-run — so a clone in that set is refused before git touches it, under
    whichever path the job reached it.

    Refuses (:class:`RepoRefused`) rather than repairing: a tracked-dirty tree
    (never stashed — the #590 rule), an ``origin`` that is not the one the job
    records, a forge that cannot be reached, a default branch that cannot
    fast-forward, a directory that is no clone. A job that recorded no origin
    predates this sync (or came from a repo with no remote) and is returned
    untouched.
    """
    from sdlc.build import dirty_tree_paths

    recorded = _recorded_origin(job)
    declared = Path(job.repo)
    if recorded is None:
        return PreparedRepo(path=declared, sha=None)

    root = work_dir if work_dir is not None else default_work_dir()
    # Resolved, because a run registers under its resolved cwd and the scheduler
    # matches the two to link the job to its run.
    target = (declared if declared.exists() else root / declared.name).resolve()
    if str(target) in busy_repos:
        raise RepoRefused(f"repo busy: {target} is already running a job", retryable=True)
    if not target.exists():
        _clone(recorded, target)
    elif not (target / ".git").exists():
        raise RepoRefused(f"{target} exists but is not a git clone")

    actual = repo_origin(target)
    if actual is None or not same_origin(actual, recorded):
        # The reason is shared with every queue client: no credential in it.
        raise RepoRefused(
            f"origin mismatch: {target} has origin "
            f"{_without_credentials(actual) if actual else '(none)'} "
            f"but the job records {_without_credentials(recorded)}"
        )
    if paths := dirty_tree_paths(target):
        raise _dirty_refusal(target, paths)

    fetch = _run_git(target, "fetch", "origin")
    if fetch.returncode != 0:
        # The forge, not this clone: down, rebooting, a credential to renew.
        # That clears without anyone touching the job, so it is retried.
        raise RepoRefused(
            f"could not fetch origin in {target}: {(fetch.stderr or fetch.stdout).strip()}",
            retryable=True,
        )
    branch = _default_branch(target)
    steps: tuple[tuple[str, ...], ...] = (
        ("checkout", "-q", branch), ("merge", "--ff-only", f"origin/{branch}"),
    )
    for args in steps:
        res = _run_git(target, *args)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip()
            hint = _diverged_hint(target, branch) if args[0] == "merge" else ""
            step = f"fast-forward to origin/{branch}" if args[0] == "merge" else " ".join(args)
            raise RepoRefused(f"could not {step} in {target}{hint}: {detail}")
    head = _run_git(target, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise RepoRefused(f"could not read HEAD of {target} after syncing")
    return PreparedRepo(path=target, sha=head.stdout.strip())
