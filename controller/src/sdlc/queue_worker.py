# ABOUTME: What a fleet worker advertises to the queue service — host, pools, harnesses,
# ABOUTME: sandbox and the clones under ~/Work. Story 35.2-001; registered by `queue run --worker`.
# ABOUTME: Also the repo auto-sync that runs before dispatch (Story 35.2-002), kept non-interactive
# ABOUTME: through the forge CLI's credential helper and a stall watchdog (Story 35.2-006).

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Iterator, Mapping, Protocol
from urllib.parse import urlparse

from sdlc.registry import normalize_dashboard_url

if TYPE_CHECKING:
    from sdlc.queue import JobRecord, QueueBackend, WorkerRecord

__all__ = [
    "ForgeUnauthenticated",
    "ForgeUnavailable",
    "GitAccess",
    "PreparedRepo",
    "RepoBusy",
    "RepoPreparer",
    "RepoRefused",
    "SyncStalled",
    "WorkerProfile",
    "clone_forge_hosts",
    "default_work_dir",
    "detect_worker_profile",
    "forge_cli",
    "forge_credential_ok",
    "forge_git_access",
    "origin_requirements",
    "prepare_repo",
    "same_origin",
    "sync_origin",
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
    # ``{forge host: the forge CLI can authenticate git there non-interactively}`` for the
    # forges its clones sit on (Story 35.2-006); a host that is False is never offered a job.
    forges: dict[str, bool] = field(default_factory=dict)

    def register_with(
        self, queue: "QueueBackend", *, slots: int, slots_free: int, **extra: Any
    ) -> "WorkerRecord":
        """Register on ``queue``; calling it again is the heartbeat.

        ``extra`` passes through to the backend's ``register_worker`` — the local
        store takes a ``now`` so a scheduler on a fake clock stays coherent.
        """
        # Only when there is something to say: a backend that predates the flag keeps working.
        if self.forges:
            extra.setdefault("forges", self.forges)
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
    forge_probe: Callable[[str, str], bool] | None = None,
) -> WorkerProfile:
    """Probe this machine for what it can run.

    ``probe`` is doctor's dependency probe (a tool on PATH that answers
    ``--version``) and ``runtime`` the sandbox's container-runtime detection —
    both injectable so a test never depends on the machine it runs on. Pools are
    declared, not detected: they name a subscription, and only the operator
    knows which one this machine is logged into. A declared :data:`CODEX_POOL`
    is still advertised only while the ``codex`` harness is (Story 35.2-008): a
    codex stage needs both, so a unit may declare the pool before Codex is
    installed and the box joins it by installing Codex, not by editing the unit.
    ``forge_probe`` answers, per forge host its clones sit on, whether the forge
    CLI is logged in there.
    """
    from sdlc.dispatch import SandboxUnavailableError, detect_container_runtime
    from sdlc.doctor import _default_dep_probe
    from sdlc.queue import CODEX_POOL

    name = name.strip()
    if not name:
        raise ValueError("worker name must not be blank")
    declared = [pool.strip() for pool in pools]
    if any(not pool for pool in declared):
        raise ValueError("a pool name must not be blank")

    origin = normalize_dashboard_url(dashboard_url) if dashboard_url is not None else None

    check = probe or _default_dep_probe
    harnesses = [harness for harness, binary in _harness_binaries().items() if check(binary)]
    if "codex" not in harnesses:
        declared = [pool for pool in declared if pool != CODEX_POOL]
    try:
        sandbox: str | None = (runtime or detect_container_runtime)()
    except SandboxUnavailableError:
        sandbox = None
    clones_dir = work_dir if work_dir is not None else default_work_dir()
    can_authenticate = forge_probe or forge_credential_ok
    return WorkerProfile(
        name=name,
        host=host or socket.gethostname().split(".")[0],
        pools=list(dict.fromkeys(declared)),
        harnesses=harnesses,
        sandbox=sandbox,
        repos=_clones(clones_dir),
        dashboard_url=origin,
        forges={
            forge: can_authenticate(forge, scheme)
            for forge, scheme in clone_forge_hosts(clones_dir).items()
        },
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

# A git call that prints nothing for this long is stalled, not slow (Story 35.2-006):
# `--progress` makes a healthy fetch or clone talk throughout, so silence means it is
# parked behind something nobody can answer — a Keychain dialog on a screen no one
# watches was the first. Kept well inside the 90 s lease the sync's keepalive extends.
_STALL_SECONDS = 60

# Grace between SIGTERM and SIGKILL when a watched git call is ended.
_KILL_GRACE_SECONDS = 2

# How often a worker blocked on a fetch or clone still beats and renews its
# leases — well inside the 30 s heartbeat, since the call can outlast the 90 s
# lease and the 90 s offline window on its own.
_KEEPALIVE_SECONDS = 10


_STALLED_REASON = "repo sync stalled"


class RepoRefused(Exception):
    """The worker will not run this job against its clone.

    ``retryable`` separates the refusals that clear without an operator — a
    dirty checkout its owner tidies, a clone another live job or run is using
    (:class:`RepoBusy`), a forge that is slow or unreachable (a fetch or clone
    that failed or timed out: :class:`ForgeUnavailable`), a lock another git
    process holds; the job goes back to ``queued`` — from the ones that need an
    operator decision (wrong origin, a default branch that cannot check out or
    fast-forward, a directory that is no clone — the job is parked
    ``blocked``).
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class ForgeUnavailable(RepoRefused):
    """A fetch or clone of the job's origin failed or timed out: the forge, not this clone.

    Down, rebooting, unreachable, a credential to renew — it clears without
    anyone touching the job. Every job on that origin would fail the same way,
    so the scheduler holds them back together rather than probing a down forge
    once per job.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason, retryable=True)


class ForgeUnauthenticated(ForgeUnavailable):
    """The forge CLI has no credential for ``host`` on this worker: it cannot authenticate git.

    Retryable like any forge outage — an operator's ``glab auth login`` clears it —
    but the scheduler also flags the host, so the matcher stops offering this
    worker the forge's jobs until the login is there.
    """

    def __init__(
        self, host: str, cli: str, scheme: str = "https", *, worker: str | None = None
    ) -> None:
        who = f"worker {worker}" if worker else "this worker"
        super().__init__(
            f"{who} cannot authenticate to {host} ({cli} auth login --hostname {host})"
        )
        self.host = host
        self.cli = cli
        self.scheme = scheme


class SyncStalled(Exception):
    """A watched git process printed nothing for ``seconds``; it has been killed."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"no output for {seconds:g}s")
        self.seconds = seconds


class RepoBusy(RepoRefused):
    """The clone at ``repo`` is in use by another live job or run; retry once it is free."""

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"repo busy: {repo} is in use by another live job or run on this host",
            retryable=True,
        )
        self.repo = repo


@dataclass(frozen=True)
class PreparedRepo:
    """Where the job runs, and the sha its tree was brought to (``None`` = not synced)."""

    path: Path
    sha: str | None


class RepoPreparer(Protocol):
    """The shape of :func:`prepare_repo`, the scheduler's injectable sync seam."""

    def __call__(
        self,
        job: "JobRecord",
        *,
        busy_repos: Collection[str] = ...,
        keepalive: Callable[[], None] | None = ...,
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


@dataclass(frozen=True)
class GitAccess:
    """How git reaches an ``http(s)`` forge without a UI: the forge CLI is its credential helper."""

    cli: str
    host: str
    scheme: str
    config: list[str]  # ``-c`` pairs, for in front of the git subcommand
    env: dict[str, str]


def forge_cli(host: str) -> str:
    """The forge CLI that vouches for ``host`` to git: ``gh`` for GitHub, ``glab`` otherwise."""
    return "gh" if host == "github.com" or host.startswith("github.") else "glab"


def forge_git_access(origin: str) -> GitAccess | None:
    """The credential helper and environment for git against ``origin``; ``None`` if not http(s).

    The inherited helpers are cleared first (``credential.helper=``) so the user's
    interactive ``osxkeychain`` can never be asked, then the forge CLI is installed
    as the only one — ``gh`` for GitHub hosts, ``glab`` for the rest — with the env
    that points it at the instance, as :mod:`sdlc.issue_host` does for a plaintext
    GitLab. Over ssh or a local path git needs no helper, so it is left alone.
    """
    from sdlc.issue_host import github_instance_env, gitlab_instance_env
    from sdlc.queue import origin_forge_host

    host = origin_forge_host(origin)
    if host is None:
        return None
    scheme = urlparse(origin.strip()).scheme
    cli = forge_cli(host)
    instance = f"{scheme}://{host}"
    cli_env = github_instance_env(instance) if cli == "gh" else gitlab_instance_env(instance)
    return GitAccess(
        cli=cli,
        host=host,
        scheme=scheme,
        config=["-c", "credential.helper=", "-c", f"credential.helper=!{cli} auth git-credential"],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **cli_env},
    )


def forge_credential_ok(
    host: str,
    scheme: str = "https",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 10,
) -> bool:
    """Whether the forge CLI would hand git a credential for ``host`` without any UI.

    Asks the CLI the question git asks it (``<cli> auth git-credential get``), so a
    CLI that is missing, logged out, or hangs reads as no — and it is a local read of
    the CLI's config, not a request to the forge, so it is fast offline.
    """
    access = forge_git_access(f"{scheme}://{host}/")
    if access is None:
        return False
    try:
        done = runner(
            [access.cli, "auth", "git-credential", "get"],
            input=f"protocol={scheme}\nhost={host}\n\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            env=access.env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "password=" in done.stdout


def clone_forge_hosts(work_dir: Path) -> dict[str, str]:
    """``{forge host: scheme}`` for the ``http(s)`` origins of the clones under ``work_dir``."""
    from sdlc.issue_host import _remote_url
    from sdlc.queue import origin_forge_host

    hosts: dict[str, str] = {}
    for name in _clones(work_dir):
        origin = _remote_url(work_dir / name)
        host = origin_forge_host(origin) if origin else None
        if origin and host:
            hosts.setdefault(host, urlparse(origin).scheme)
    return hosts


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    """End ``proc`` and everything it started: SIGTERM the group, then SIGKILL what is left."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass
    try:
        # The leader may have exited on TERM while a helper it started did not.
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _run_group(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: float = _GIT_TIMEOUT_SECONDS,
    stall_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in its own process group, ended if it goes quiet or overruns ``timeout``.

    ``subprocess.run`` cannot tell a slow git from one waiting on a dialog nobody
    sees; this reads stdout and stderr as they arrive. No output for
    ``stall_seconds`` raises :class:`SyncStalled`, and ``timeout`` overall raises
    ``subprocess.TimeoutExpired`` — either way the whole group is killed (a credential
    helper is a child of git), so nothing is left holding the clone.
    """
    quiet_limit = _STALL_SECONDS if stall_seconds is None else stall_seconds
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=True,
    )
    started = last_output = time.monotonic()
    output: dict[str, list[bytes]] = {"out": [], "err": []}

    def pump(stream: Any, sink: list[bytes]) -> None:
        nonlocal last_output
        for chunk in iter(lambda: stream.read1(65536), b""):
            sink.append(chunk)
            last_output = time.monotonic()

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, output["out"]), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, output["err"]), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now - started > timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            if now - last_output > quiet_limit:
                raise SyncStalled(quiet_limit)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        # A helper that outlives git can hold the pipes open; do not wait on it for ever.
        for reader in readers:
            reader.join(timeout=_KILL_GRACE_SECONDS)
    return subprocess.CompletedProcess(
        argv,
        proc.returncode,
        b"".join(output["out"]).decode(errors="replace"),
        b"".join(output["err"]).decode(errors="replace"),
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    config: list[str] = []
    if args[0] == "fetch":
        # The one network call here: under the forge CLI's helper, and talking while it
        # works so the watchdog can tell it from a hang.
        from sdlc.issue_host import _remote_url

        origin = _remote_url(root)
        access = forge_git_access(origin) if origin else None
        if access is not None:
            config, env = access.config, access.env
        argv.insert(1, "--progress")
    return _run_group(["git", "-C", str(root), *config, *argv], env=env)


@contextmanager
def _kept_alive(keepalive: Callable[[], None] | None) -> Iterator[None]:
    """Call ``keepalive`` every :data:`_KEEPALIVE_SECONDS` while the block runs.

    A fetch or clone may outlast the worker's lease and offline window, and the
    drain is blocked on it, so a side thread keeps the worker beating and its
    leases renewed meanwhile — the way `dispatch` watches a blocking agent. The
    drain thread is parked in the git call throughout, and the side thread is
    joined before the block exits, so the two never act on the scheduler at once.
    """
    if keepalive is None:
        yield
        return
    done = threading.Event()

    def beat() -> None:
        while not done.wait(_KEEPALIVE_SECONDS):
            keepalive()

    thread = threading.Thread(target=beat, name="sdlc-sync-keepalive", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()


def origin_requirements(root: Path) -> str | None:
    """The ``requirements`` JSON an enqueue records for ``root`` (``None`` without an origin).

    The origin is recorded without credentials, exactly as a fleet job's is
    (35.3-001's :func:`~sdlc.issue_host.strip_remote_credentials`): a job never
    carries a token, and a worker clones with its own.
    """
    from sdlc.issue_host import _remote_url, strip_remote_credentials

    origin = _remote_url(root)
    return json.dumps({"origin": strip_remote_credentials(origin)}) if origin else None


def _recorded_origin(job: "JobRecord") -> str | None:
    if not job.requirements:
        return None
    try:
        data = json.loads(job.requirements)
    except ValueError:
        return None
    value = data.get("origin") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def sync_origin(job: "JobRecord") -> str | None:
    """The forge ``job``'s sync fetches from, as ``host/path``; ``None`` if it is never synced.

    Spelled the way :func:`same_origin` compares, so two jobs naming one repo
    by different URLs (ssh on one machine, https on another) share one forge.
    """
    recorded = _recorded_origin(job)
    return _origin_key(recorded) if recorded is not None else None


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """:func:`_git`, with a timeout (retryable) or missing git surfaced as a refusal."""
    try:
        return _git(root, *args)
    except subprocess.TimeoutExpired as exc:
        raise RepoRefused(f"git {args[0]} timed out in {root}: {exc}", retryable=True) from exc
    except SyncStalled as exc:
        raise RepoRefused(_STALLED_REASON, retryable=True) from exc
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


# What git says when the credential helper had nothing to give and prompting is off.
_AUTH_FAILURE = re.compile(
    r"terminal prompts disabled|could not read (?:Username|Password)|Authentication failed"
    r"|HTTP Basic: Access denied|returned error: 401",
    re.IGNORECASE,
)


def _is_auth_failure(stderr: str) -> bool:
    return bool(_AUTH_FAILURE.search(stderr))


def _clone(origin: str, target: Path) -> None:
    from sdlc.issue_host import strip_remote_credentials

    shown = strip_remote_credentials(origin)
    # The origin comes from a job body; one shaped like an option
    # (`--upload-pack=…`) must never reach git's argument parser.
    if origin.startswith("-"):
        raise RepoRefused(f"refusing to clone {shown!r}: an origin that reads as a git option")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # This host's to fix, not the forge's — and the drain catches refusals
        # only, so an `OSError` let through here would stop the whole worker.
        raise RepoRefused(f"could not clone {shown} into {target}: {exc}") from exc
    access = forge_git_access(origin)
    try:
        # The forge CLI is the credential helper (see `forge_git_access`), so no
        # token is handled here and no Keychain dialog can open. `--progress` keeps
        # a healthy clone talking, which is what the stall watchdog listens for.
        res = _run_group(
            [
                "git", *(access.config if access else []),
                "clone", "-q", "--progress", "--", origin, str(target),
            ],
            env=access.env if access else {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except SyncStalled as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ForgeUnavailable(_STALLED_REASON) from exc
    except subprocess.TimeoutExpired as exc:
        # The timeout SIGKILLs git, so it cannot remove its half-written clone —
        # which the retry would otherwise find and judge as a (dirty) clone.
        shutil.rmtree(target, ignore_errors=True)
        # Not `exc`: its text is the argv, origin and all.
        raise ForgeUnavailable(
            f"could not clone {shown} into {target}: "
            f"timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoRefused(f"could not clone {shown} into {target}: {exc}") from exc
    except BaseException:
        # Ctrl-C mid-clone: `subprocess.run` SIGKILLs git on the way out, so the
        # half-written clone is ours to remove. The target did not exist before.
        shutil.rmtree(target, ignore_errors=True)
        raise
    if res.returncode != 0:
        if access is not None and _is_auth_failure(res.stderr):
            shutil.rmtree(target, ignore_errors=True)
            raise ForgeUnauthenticated(access.host, access.cli, access.scheme)
        # The forge, not the job: down, rebooting, a credential to renew. That
        # clears without anyone touching the job, so it goes back to be retried.
        raise ForgeUnavailable(
            f"could not clone {shown} into {target}: "
            f"{res.stderr.strip() or 'git clone failed'}"
        )


def prepare_repo(
    job: "JobRecord",
    *,
    work_dir: Path | None = None,
    busy_repos: Collection[str] = (),
    keepalive: Callable[[], None] | None = None,
) -> PreparedRepo:
    """Bring the job's clone to the forge's default branch before it is dispatched.

    ``git fetch origin && git checkout -q main && git merge --ff-only
    origin/main``, then the resulting sha — with the branch ``origin/HEAD``
    names in place of ``main`` when the forge's default is another. The clone
    is the job's own path when that exists on this machine, otherwise
    ``work_dir/<name>`` (``~/Work``): a path recorded on another machine means
    nothing here. An absent clone is made from the origin the job recorded at
    enqueue.

    ``busy_repos`` are the clones another live job or run on this host is
    using. The sync checks out and fast-forwards the clone itself — a write to
    the shared checkout, which the claim's build/build overlap (Story 32.1-003)
    assumes no job makes mid-run, and under the feet of a `sdlc fix` working in
    it — so a clone in that set is refused (:class:`RepoBusy`) before git
    touches it, under whichever path the job reached it.

    ``keepalive`` is called every :data:`_KEEPALIVE_SECONDS` while the clone or
    the fetch is in flight: either can outlast the worker's lease.

    Refuses (:class:`RepoRefused`) rather than repairing: a tracked-dirty tree
    (never stashed — the #590 rule), an ``origin`` that is not the one the job
    records, a forge that cannot be reached, a default branch that cannot
    fast-forward, a directory that is no clone. A job that recorded no origin
    predates this sync (or came from a repo with no remote) and is returned
    untouched.
    """
    from sdlc.build import dirty_tree_paths
    from sdlc.issue_host import _remote_url, strip_remote_credentials

    recorded = _recorded_origin(job)
    declared = Path(job.repo)
    if recorded is None:
        return PreparedRepo(path=declared, sha=None)

    root = work_dir if work_dir is not None else default_work_dir()
    # Resolved, because a run registers under its resolved cwd and the scheduler
    # matches the two to link the job to its run.
    target = (declared if declared.exists() else root / declared.name).resolve()
    if str(target) in busy_repos:
        raise RepoBusy(str(target))
    if not target.exists():
        with _kept_alive(keepalive):
            _clone(recorded, target)
    elif not (target / ".git").exists():
        raise RepoRefused(f"{target} exists but is not a git clone")

    actual = _remote_url(target)
    if not actual or not same_origin(actual, recorded):
        # The reason is shared with every queue client: no credential in it.
        raise RepoRefused(
            f"origin mismatch: {target} has origin "
            f"{strip_remote_credentials(actual) if actual else '(none)'} "
            f"but the job records {strip_remote_credentials(recorded)}"
        )
    if paths := dirty_tree_paths(target):
        raise _dirty_refusal(target, paths)

    try:
        with _kept_alive(keepalive):
            fetch = _run_git(target, "fetch", "origin")
    except RepoRefused as exc:
        # A timeout is a forge that hangs, not this clone; git that cannot run
        # at all is this host's to fix, and stays a refusal that parks the job.
        if exc.retryable:
            raise ForgeUnavailable(exc.reason) from exc
        raise
    if fetch.returncode != 0:
        access = forge_git_access(actual)
        if access is not None and _is_auth_failure(fetch.stderr or fetch.stdout):
            raise ForgeUnauthenticated(access.host, access.cli, access.scheme)
        # The forge, not this clone: down, rebooting, a credential to renew.
        # That clears without anyone touching the job, so it is retried.
        raise ForgeUnavailable(
            f"could not fetch origin in {target}: {(fetch.stderr or fetch.stdout).strip()}"
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
            # Another git process holding the index or a ref (an IDE refreshing
            # its status, say) lets go on its own: retry, rather than park the
            # job for an operator. Git quotes the lock's path in any language.
            raise RepoRefused(
                f"could not {step} in {target}{hint}: {detail}",
                retryable=".lock'" in detail,
            )
    head = _run_git(target, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise RepoRefused(f"could not read HEAD of {target} after syncing")
    return PreparedRepo(path=target, sha=head.stdout.strip())
