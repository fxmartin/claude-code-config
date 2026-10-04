# ABOUTME: `sdlc queue serve` — the host queue store behind a tailnet-only HTTP API.
# ABOUTME: Story 35.1-001. Stdlib ThreadingHTTPServer; identity via `tailscale whois`.

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

from sdlc import __version__
from sdlc.queue import JobRecord, QueueError, QueueStore, WorkerRecord
from sdlc.registry import RunRecord, normalize_dashboard_url
from sdlc.scheduler import DEFAULT_LEASE_SECONDS

__all__ = [
    "LOOPBACK_NETWORKS",
    "TAILNET_NETWORKS",
    "AccessPolicy",
    "BindError",
    "Decision",
    "WhoisUnavailable",
    "make_server",
    "parse_bind",
    "serve",
    "tailscale_whois",
]

logger = logging.getLogger("sdlc.queue_server")

# Tailscale's address space: the CGNAT block IPv4 nodes live in and the ULA
# prefix IPv6 nodes live in. A peer outside these is not on the tailnet.
TAILNET_NETWORKS: tuple[str, ...] = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
LOOPBACK_NETWORKS: tuple[str, ...] = ("127.0.0.0/8", "::1/128")

# The service may also *bind* loopback — unreachable from the tailnet, so no
# exposure, and what makes local development and tests possible. It is never a
# valid *peer* unless the policy is built to trust it explicitly.
_BIND_NETWORKS = TAILNET_NETWORKS + LOOPBACK_NETWORKS

# Requests are small JSON documents; a megabyte is generous and bounds what an
# authorised-but-buggy client can make a handler thread buffer.
MAX_BODY_BYTES = 1024 * 1024
# How much of an oversized body is read-and-discarded before answering 413, so
# the client sees the status rather than a reset connection.
_DRAIN_LIMIT = 16 * 1024 * 1024
# Per-socket-read deadline. The identity gate only runs once headers are in, so
# without it any tailnet peer — allowlisted or not — could pin a handler thread
# with a connection that goes silent. It bounds each read, not the request: a
# peer that sends a byte inside every window still holds its thread, and only
# the tailnet-only bind limits who can do that.
_REQUEST_TIMEOUT_SECONDS = 30

WHOIS_CACHE_SECONDS = 60
_WHOIS_TIMEOUT_SECONDS = 5
_MAX_LEASE_SECONDS = 24 * 3600
_REQUIREMENT_KEYS = frozenset({"repo", "origin", "harness", "sandbox"})


class BindError(ValueError):
    """The requested bind address is not one the service may listen on."""


class WhoisUnavailable(RuntimeError):
    """``tailscale whois`` could not be asked (no CLI, daemon down, timeout)."""


# ---------------------------------------------------------------------------
# Bind rules
# ---------------------------------------------------------------------------


def _networks(specs: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(spec) for spec in specs]


def _check_bind_host(host: str) -> None:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BindError(
            f"bind host must be an IP address, not {host!r} "
            "(a hostname could resolve to a wildcard)"
        ) from exc
    if addr.is_unspecified:
        raise BindError(
            f"refusing to bind {host}: the queue service never binds all interfaces"
        )
    if not any(addr in net for net in _networks(_BIND_NETWORKS)):
        raise BindError(
            f"refusing to bind {host}: not a tailnet address "
            f"({', '.join(TAILNET_NETWORKS)}) or loopback"
        )


def parse_bind(spec: str) -> tuple[str, int]:
    """``host:port`` / ``[v6]:port`` → ``(host, port)``, enforcing the bind rules.

    Only a tailnet address or loopback is accepted; a wildcard, a LAN/public
    address or a hostname is a :class:`BindError`. The rule lives here *and* in
    :func:`make_server`, so no caller can reach a wildcard socket by skipping the
    CLI.
    """
    host, sep, port_text = spec.rpartition(":")
    if not sep or not host or not port_text:
        raise BindError(f"bind must be <ip>:<port>, got {spec!r}")
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        port = int(port_text)
    except ValueError as exc:
        raise BindError(f"bind port must be a number, got {port_text!r}") from exc
    if not 0 <= port <= 65535:
        raise BindError(f"bind port out of range: {port}")
    _check_bind_host(host)
    return host, port


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def tailscale_whois(ip: str) -> str | None:
    """The Tailscale login name behind ``ip``, or ``None`` when it has none.

    Raises :class:`WhoisUnavailable` when the CLI cannot be asked at all — the
    caller must treat that as "cannot vouch", never as "allowed".
    """
    try:
        proc = subprocess.run(
            ["tailscale", "whois", "--json", ip],
            capture_output=True,
            text=True,
            timeout=_WHOIS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WhoisUnavailable(f"tailscale whois failed: {exc}") from exc
    if proc.returncode != 0:
        raise WhoisUnavailable(f"tailscale whois exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
        login = payload["UserProfile"]["LoginName"]
    except (ValueError, KeyError, TypeError):
        return None
    return login if isinstance(login, str) and login else None


@dataclass(frozen=True)
class Decision:
    """Whether a request may proceed, and who it was or why not (for the log)."""

    allowed: bool
    detail: str


class AccessPolicy:
    """Who may talk to the service: tailnet peers whose identity is vouched for.

    Three gates, in order. (1) The peer address must lie inside ``networks`` —
    the tailnet's by default — so a request that arrives any other way is
    refused before identity is even asked. (2) A valid ``token`` (the
    ``SDLC_QUEUE_TOKEN`` shared secret, sent as ``Authorization: Bearer``)
    admits a peer on a host without the Tailscale CLI. (3) Otherwise the
    peer's Tailscale login name (``tailscale whois``) must be in ``allow``.

    Everything fails closed: an empty allowlist plus no token is refused at
    construction, and a ``whois`` that errors admits nobody. Lookups are cached
    per IP for :data:`WHOIS_CACHE_SECONDS`; errors are not, so a flapping
    daemon recovers on the next request.
    """

    def __init__(
        self,
        *,
        allow: Iterable[str] = (),
        token: str | None = None,
        networks: Iterable[str] = TAILNET_NETWORKS,
        whois: Callable[[str], str | None] = tailscale_whois,
        ttl: float = WHOIS_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allow = frozenset(entry.strip().lower() for entry in allow if entry.strip())
        self._token = token or None
        if not self._allow and not self._token:
            raise ValueError(
                "access policy needs an identity allowlist (--allow / SDLC_QUEUE_ALLOW) "
                "or a token (SDLC_QUEUE_TOKEN); refusing to serve everyone"
            )
        if self._token and not self._token.isascii():
            # http.server decodes header values as latin-1, so a non-ASCII
            # secret could never match what a client sends: refuse it up front
            # rather than serve a token gate that admits nobody.
            raise ValueError("SDLC_QUEUE_TOKEN must be ASCII")
        self._networks = _networks(networks)
        self._whois = whois
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def authorize(self, peer_ip: str, bearer: str | None) -> Decision:
        try:
            addr = ipaddress.ip_address(peer_ip.split("%", 1)[0])
        except ValueError:
            return Decision(False, f"unparseable peer address {peer_ip!r}")
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if not any(addr in net for net in self._networks):
            return Decision(False, "peer is not on the tailnet")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str, and a
        # peer chooses what its Authorization header carries.
        if (
            self._token
            and bearer is not None
            and hmac.compare_digest(bearer.encode(), self._token.encode())
        ):
            return Decision(True, "token")
        if not self._allow:
            return Decision(False, "no valid token and no identity allowlist")
        try:
            login = self._login_for(str(addr))
        except WhoisUnavailable as exc:
            return Decision(False, f"identity unavailable: {exc}")
        if login is None:
            return Decision(False, "peer has no Tailscale identity")
        if login.lower() not in self._allow:
            return Decision(False, f"identity {login!r} is not in the allowlist")
        return Decision(True, login)

    def _login_for(self, ip: str) -> str | None:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(ip)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        login = self._whois(ip)  # outside the lock: a slow CLI must not stall peers
        with self._lock:
            self._cache[ip] = (now, login)
        return login


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


# Control characters as \xNN, as http.server's own log_message writes them
# (gh-100001): request lines come from peers the gate has not vouched for yet.
_CONTROL_ESCAPES = {c: rf"\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))}
_CONTROL_ESCAPES[ord("\\")] = r"\\"


def _printable(text: str) -> str:
    return text.translate(_CONTROL_ESCAPES)


class _ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


Body = dict[str, Any]
Reply = tuple[int, Any]


def _text(body: Body, key: str, *, required: bool = False) -> str | None:
    value = body.get(key)
    if value is None:
        if required:
            raise _ApiError(400, f"{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise _ApiError(400, f"{key} must be a non-empty string")
    return value


def _required(body: Body, key: str) -> str:
    value = _text(body, key, required=True)
    assert value is not None
    return value


def _lease(body: Body) -> int:
    value = body.get("lease_seconds", DEFAULT_LEASE_SECONDS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LEASE_SECONDS:
        raise _ApiError(400, f"lease_seconds must be an integer in 1..{_MAX_LEASE_SECONDS}")
    return value


def _string_list(body: Body, key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _ApiError(400, f"{key} must be a list of strings")
    return value


def _int(body: Body, key: str, *, default: int | None = None) -> int | None:
    value = body.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ApiError(400, f"{key} must be an integer")
    return value


def _timestamp(body: Body, key: str) -> datetime | None:
    """``body[key]`` as an aware datetime; absent or ``null`` is ``None`` (a naive one is UTC)."""
    raw = body.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _ApiError(400, f"{key} must be an ISO-8601 timestamp or null")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _ApiError(400, f"{key} must be an ISO-8601 timestamp, got {raw!r}") from exc
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _requirements_json(body: Body) -> str | None:
    value = body.get("requirements")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not set(value) <= _REQUIREMENT_KEYS
        or not all(isinstance(v, str) for v in value.values())
    ):
        raise _ApiError(
            400, f"requirements must be an object of strings with keys in {sorted(_REQUIREMENT_KEYS)}"
        )
    return json.dumps(value, sort_keys=True)


def _job(store: QueueStore, job_id: int) -> JobRecord:
    job = store.get_job(job_id)
    if job is None:
        raise _ApiError(404, f"unknown job id: {job_id}")
    return job


def _held_by(job: JobRecord, worker: str) -> None:
    if job.claimed_by != worker:
        raise _ApiError(409, f"job {job.id} is not claimed by {worker!r}")


class _Routes:
    """One method per endpoint, each a thin mapping onto the same-named store verb."""

    def __init__(self, store: QueueStore, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._clock = clock

    def _now(self) -> datetime | None:
        """The service's own time for every lease decision — one clock for the whole fleet.

        ``None`` lets the store read the wall clock; a test injects one so a lease
        can lapse without sleeping.
        """
        return self._clock() if self._clock is not None else None

    # --- reads ---------------------------------------------------------

    def health(self, _query: Any, _body: Body) -> Reply:
        # Reaching a handler means the identity gate already admitted the caller,
        # so a 200 here is `sdlc doctor`'s "reachable, identity accepted".
        return 200, {"ok": True, "controller_version": __version__}

    def list_jobs(self, query: dict[str, list[str]], _body: Body) -> Reply:
        # Same envelope `sdlc queue list --json` emits: the pause lives beside
        # the jobs, not in one of them, and an elapsed window is not state.
        repo = query.get("repo", [None])[0]
        pauses = [p for p in self.store.dispatch_pauses() if p.is_active(self._now())]
        jobs = self.store.list_jobs(repo)
        return 200, {
            # `pause` is the first live window (the one-pool shape of Story
            # 32.2-001); `pauses` is one entry per paused pool (Story 35.2-003).
            "pause": pauses[0].to_dict() if pauses else None,
            "pauses": [p.to_dict() for p in pauses],
            "jobs": [job.to_dict() for job in jobs],
        }

    # --- job writers ---------------------------------------------------

    def add_job(self, _query: Any, body: Body) -> Reply:
        options = _string_list(body, "options")
        try:
            job_id = self.store.add_job(
                repo=_required(body, "repo"),
                kind=_required(body, "kind"),
                scope=_required(body, "scope"),
                priority=_text(body, "priority"),
                options_json=json.dumps(options) if options is not None else None,
                labels=_string_list(body, "labels") or (),
                host=_text(body, "host"),
                pool=_text(body, "pool"),
                requirements_json=_requirements_json(body),
            )
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
        # Say at once, rather than at the next heartbeat, when no worker can run it.
        self.store.stamp_unsatisfiable()
        return 201, _job(self.store, job_id).to_dict()

    # --- workers (Story 35.2-001) --------------------------------------

    def register_worker(self, _query: Any, body: Body) -> Reply:
        """Register a worker; calling it again is the heartbeat."""
        sandbox = _text(body, "sandbox")
        try:
            record = self.store.register_worker(
                _required(body, "worker"),
                host=_required(body, "host"),
                pools=_string_list(body, "pools") or (),
                harnesses=_string_list(body, "harnesses") or (),
                sandbox=sandbox,
                repos=_string_list(body, "repos") or (),
                slots=_int(body, "slots", default=1) or 0,
                slots_free=_int(body, "slots_free"),
                now=self._now(),
            )
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
        return 200, record.to_dict()

    def list_workers(self, _query: Any, _body: Body) -> Reply:
        return 200, {"workers": [worker.to_dict() for worker in self.store.list_workers()]}

    # --- the fleet run registry (Story 35.4-001) -----------------------

    def put_run(self, _query: Any, body: Body) -> Reply:
        """Upsert one run's record, as a build pushes it on start and finish."""
        total = _int(body, "total")
        completed = _int(body, "completed")
        pid = _int(body, "pid")
        dashboard_url = _text(body, "dashboard_url")
        if dashboard_url is not None:
            try:
                dashboard_url = normalize_dashboard_url(dashboard_url)
            except ValueError as exc:
                raise _ApiError(400, str(exc)) from exc
        record = RunRecord(
            run_id=_required(body, "run_id"),
            repo=_required(body, "repo"),
            db=_text(body, "db") or "",
            scope=_required(body, "scope"),
            pid=pid if pid is not None else 0,
            status=_required(body, "status"),
            started_at=_required(body, "started_at"),
            finished_at=_text(body, "finished_at"),
            total=total,
            completed=completed,
            worker=_required(body, "worker"),
            dashboard_url=dashboard_url,
            origin=_text(body, "origin"),
        )
        try:
            self.store.put_fleet_run(record, now=self._now())
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
        return 200, {"ok": True}

    def list_runs(self, _query: Any, _body: Body) -> Reply:
        """Every pushed run, each flagged with whether its worker is still heartbeating.

        ``worker_online`` is ``None`` for a worker that never registered — no
        heartbeat to judge by — so a reader does not mistake "unknown" for "gone".
        """
        now = self._now()
        online = {w.name: w.is_online(now) for w in self.store.list_workers()}
        runs = [
            {**row, "worker_online": online.get(row["worker"])}
            for row in self.store.list_fleet_runs()
        ]
        return 200, {"runs": runs}

    def claim(self, _query: Any, body: Body) -> Reply:
        claimed = self.store.claim_next(
            claimed_by=_required(body, "worker"),
            lease_seconds=_lease(body),
            host=_text(body, "host"),
            pools=_string_list(body, "pools"),
            now=self._now(),
        )
        return (200, claimed.to_dict()) if claimed is not None else (204, None)

    def renew(self, job_id: int, body: Body) -> Reply:
        worker = _required(body, "worker")
        job = _job(self.store, job_id)
        if not self.store.renew_lease(
            job_id, claimed_by=worker, lease_seconds=_lease(body), now=self._now()
        ):
            raise _ApiError(409, f"job {job.id} is not claimed by {worker!r}")
        return 200, _job(self.store, job_id).to_dict()

    def release(self, job_id: int, body: Body) -> Reply:
        worker = _required(body, "worker")
        _held_by(_job(self.store, job_id), worker)
        self.store.release_claim(
            job_id, claimed_by=worker, reason=_text(body, "reason"), now=self._now()
        )
        return 200, _job(self.store, job_id).to_dict()

    def finish(self, job_id: int, body: Body) -> Reply:
        job = _job(self.store, job_id)
        state = _required(body, "state")
        worker = _text(body, "worker")
        try:
            # The holder check rides in the UPDATE itself: checked on a read
            # first, a local reclaim could land in between and be overwritten.
            finished = self.store.finish_job(
                job_id, state, reason=_text(body, "reason"), claimed_by=worker
            )
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
        if not finished:
            raise _ApiError(409, f"job {job.id} is not claimed by {worker!r}")
        return 200, _job(self.store, job_id).to_dict()

    def cancel(self, job_id: int, _body: Body) -> Reply:
        _job(self.store, job_id)
        try:
            self.store.cancel_job(job_id)
        except QueueError as exc:
            raise _ApiError(409, str(exc)) from exc
        return 200, _job(self.store, job_id).to_dict()

    def requeue(self, job_id: int, _body: Body) -> Reply:
        _job(self.store, job_id)
        try:
            self.store.requeue_job(job_id, now=self._now())
        except QueueError as exc:
            raise _ApiError(409, str(exc)) from exc
        return 200, _job(self.store, job_id).to_dict()

    def prioritise(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        try:
            self.store.prioritise_job(job_id, _required(body, "priority"))
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
        return 200, _job(self.store, job_id).to_dict()

    # --- the scheduler's surface (Story 35.2-005) ----------------------
    #
    # One route per store verb `sdlc queue run` uses, so a worker drains this
    # queue through the QueueClient the way a local drain uses the store. The
    # lease verbs answer 409 when the guarded UPDATE lost — the loser's normal
    # outcome, not an error — and every "now" is this service's own clock.

    def peek_claimable(self, query: dict[str, list[str]], _body: Body) -> Reply:
        jobs = self.store.peek_claimable(
            busy_repos=set(query.get("busy", ())),
            fix_busy_repos=set(query.get("fix_busy", ())),
            now=self._now(),
        )
        return 200, {"jobs": [job.to_dict() for job in jobs]}

    def eligible(self, _query: Any, body: Body) -> Reply:
        """Which of ``job_ids`` (dispatch order kept) ``worker`` may take right now."""
        ids = body.get("job_ids")
        if not isinstance(ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids
        ):
            raise _ApiError(400, "job_ids must be a list of integers")
        # A job that vanished between the caller's peek and now is simply not a candidate.
        candidates = [job for job in map(self.store.get_job, ids) if job is not None]
        kept = self.store.claimable_for_worker(
            _required(body, "worker"),
            candidates,
            slots_free=_int(body, "slots_free"),
            now=self._now(),
        )
        return 200, {"jobs": [job.to_dict() for job in kept]}

    def expired_running(self, _query: Any, _body: Body) -> Reply:
        jobs = self.store.expired_running_jobs(now=self._now())
        return 200, {"jobs": [job.to_dict() for job in jobs]}

    def due_parked(self, _query: Any, _body: Body) -> Reply:
        jobs = self.store.due_parked_jobs(now=self._now())
        return 200, {"jobs": [job.to_dict() for job in jobs]}

    def running_repos(self, query: dict[str, list[str]], _body: Body) -> Reply:
        excluding = query.get("excluding", [None])[0]
        try:
            excluded = int(excluding) if excluding is not None else None
        except ValueError as exc:
            raise _ApiError(400, "excluding must be an integer") from exc
        repos = self.store.running_repos(
            kind=query.get("kind", [None])[0] or None, excluding=excluded
        )
        return 200, {"repos": sorted(repos)}

    def overlap_holds(self, _query: Any, _body: Body) -> Reply:
        # JSON object keys are strings; the client turns them back into ints.
        return 200, {"holds": {str(job): holder for job, holder in self.store.overlap_holds().items()}}

    def get_worker(self, name: str) -> WorkerRecord:
        worker = self.store.get_worker(name)
        if worker is None:
            raise _ApiError(404, f"unknown worker: {name}")
        return worker

    def _lease_take(self, job_id: int, body: Body, verb: str) -> Reply:
        job = _job(self.store, job_id)
        taken = getattr(self.store, verb)(
            job_id,
            claimed_by=_required(body, "claimed_by"),
            lease_seconds=_lease(body),
            now=self._now(),
            worker=_text(body, "worker"),
        )
        if taken is None:
            raise _ApiError(409, f"job {job.id} could not be taken (state {job.state})")
        return 200, taken.to_dict()

    def claim_one(self, job_id: int, body: Body) -> Reply:
        return self._lease_take(job_id, body, "claim_job")

    def reclaim(self, job_id: int, body: Body) -> Reply:
        return self._lease_take(job_id, body, "reclaim_job")

    def take(self, job_id: int, body: Body) -> Reply:
        return self._lease_take(job_id, body, "take_parked_job")

    def park(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        pr_number = _int(body, "pr_number")
        if pr_number is None:
            raise _ApiError(400, "pr_number is required")
        try:
            parked = self.store.park_job(
                job_id,
                pr_number=pr_number,
                reason=_required(body, "reason"),
                poll_after=_timestamp(body, "poll_after"),
            )
        except QueueError as exc:
            raise _ApiError(409, str(exc)) from exc
        return 200, {"parked": parked, "job": _job(self.store, job_id).to_dict()}

    def poll(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        self.store.schedule_poll(job_id, _timestamp(body, "poll_after"))
        return 200, _job(self.store, job_id).to_dict()

    def reason(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        self.store.set_reason(job_id, _text(body, "reason"))
        return 200, _job(self.store, job_id).to_dict()

    def run(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        self.store.attach_run(job_id, _required(body, "run_id"))
        return 200, _job(self.store, job_id).to_dict()

    def sync(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        self.store.record_sync(job_id, repo=_required(body, "repo"), sha=_required(body, "sha"))
        return 200, _job(self.store, job_id).to_dict()

    def files(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        paths = _string_list(body, "paths")
        if paths is None:
            raise _ApiError(400, "paths is required")
        self.store.record_files(job_id, paths)
        return 200, _job(self.store, job_id).to_dict()

    def fix_rounds(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        rounds = _int(body, "rounds")
        if rounds is None:
            raise _ApiError(400, "rounds is required")
        self.store.record_fix_rounds_baseline(job_id, rounds)
        return 200, _job(self.store, job_id).to_dict()

    def restart(self, job_id: int, body: Body) -> Reply:
        _job(self.store, job_id)
        self.store.restart_fresh(job_id, reason=_required(body, "reason"), now=self._now())
        return 200, _job(self.store, job_id).to_dict()

    # --- the shared rate-limit pause -----------------------------------

    def pause(self, _query: Any, body: Body) -> Reply:
        raw = _required(body, "until")
        try:
            until = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise _ApiError(400, f"until must be an ISO-8601 timestamp, got {raw!r}") from exc
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        pool = _text(body, "pool")
        opened = self.store.pause_dispatch(
            until=until,
            reason=_text(body, "reason"),
            run_id=_text(body, "run_id"),
            repo=_text(body, "repo"),
            source=_text(body, "source"),
            pool=pool,
            now=self._now(),
        )
        recorded = self.store.dispatch_pause(pool)
        return 200, {"opened": opened, "pause": recorded.to_dict() if recorded else None}

    def pauses(self, _query: Any, _body: Body) -> Reply:
        """Every recorded window, *raw*: an elapsed one is still listed.

        The scheduler tells "the window just reopened" (announce it, clear the
        row) from "never paused" (say nothing) by that row's survival, so unlike
        the `GET /jobs` snapshot this does not filter on ``is_active``.
        """
        return 200, {"pauses": [p.to_dict() for p in self.store.dispatch_pauses()]}

    def pause_probed(self, _query: Any, body: Body) -> Reply:
        self.store.mark_pause_probed(_text(body, "pool"), now=self._now())
        return 200, {"ok": True}

    def resume(self, query: dict[str, list[str]], _body: Body) -> Reply:
        # `?pool=P` lifts one pool's window; no pool lifts every window.
        self.store.clear_pause(query.get("pool", [None])[0] or None)
        return 200, {"pause": None}


# The `/jobs/{id}/<verb>` endpoints: URL verb -> the _Routes method it maps onto.
_JOB_VERBS = {
    "renew": "renew",
    "release": "release",
    "finish": "finish",
    "cancel": "cancel",
    "requeue": "requeue",
    "prioritise": "prioritise",
    "claim": "claim_one",
    "reclaim": "reclaim",
    "take": "take",
    "park": "park",
    "poll": "poll",
    "reason": "reason",
    "run": "run",
    "sync": "sync",
    "files": "files",
    "fix-rounds": "fix_rounds",
    "restart": "restart",
}


def _route(method: str, path: str, routes: _Routes) -> Callable[[dict[str, list[str]], Body], Reply]:
    parts = [p for p in path.split("/") if p]
    table: dict[tuple[str, tuple[str, ...]], Callable[[dict[str, list[str]], Body], Reply]] = {
        ("GET", ("health",)): routes.health,
        ("GET", ("jobs",)): routes.list_jobs,
        ("POST", ("jobs",)): routes.add_job,
        ("POST", ("jobs", "claim")): routes.claim,
        ("GET", ("jobs", "claimable")): routes.peek_claimable,
        ("POST", ("jobs", "eligible")): routes.eligible,
        ("GET", ("jobs", "expired")): routes.expired_running,
        ("GET", ("jobs", "due")): routes.due_parked,
        ("GET", ("jobs", "running-repos")): routes.running_repos,
        ("GET", ("jobs", "holds")): routes.overlap_holds,
        ("GET", ("workers",)): routes.list_workers,
        ("POST", ("workers",)): routes.register_worker,
        ("GET", ("runs",)): routes.list_runs,
        ("PUT", ("runs",)): routes.put_run,
        ("GET", ("pause",)): routes.pauses,
        ("POST", ("pause",)): routes.pause,
        ("POST", ("pause", "probed")): routes.pause_probed,
        ("DELETE", ("pause",)): routes.resume,
    }
    handler = table.get((method, tuple(parts)))
    if handler is not None:
        return handler
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] in _JOB_VERBS:
        if method != "POST":
            raise _ApiError(405, f"{method} not allowed on {path}")
        try:
            job_id = int(parts[1])
        except ValueError as exc:
            raise _ApiError(404, f"no such route: {path}") from exc
        verb = getattr(routes, _JOB_VERBS[parts[2]])
        return lambda _query, body: verb(job_id, body)
    if any(key[1] == tuple(parts) for key in table):
        raise _ApiError(405, f"{method} not allowed on {path}")
    if len(parts) == 2 and parts[0] == "workers":
        if method != "GET":
            raise _ApiError(405, f"{method} not allowed on {path}")
        name = unquote(parts[1])
        return lambda _query, _body: (200, routes.get_worker(name).to_dict(routes._now()))
    raise _ApiError(404, f"no such route: {path}")


class _Handler(BaseHTTPRequestHandler):
    server: "_QueueServer"
    timeout = _REQUEST_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s - %s", self.address_string(), _printable(format % args))

    def _dispatch(self, method: str) -> None:
        peer = self.client_address[0]
        decision = self._browser_refusal()
        if decision is None:
            try:
                decision = self.server.policy.authorize(peer, self._bearer())
            except Exception as exc:  # noqa: BLE001 — the gate fails closed: a crash is a refusal
                # The type only: an encode error's repr would carry the presented secret.
                decision = Decision(False, f"authorization error ({type(exc).__name__})")
        if not decision.allowed:
            logger.warning(
                "refused %s %s from %s: %s", method, _printable(self.path), peer, decision.detail
            )
            self._send(403, {"error": "forbidden"})
            return
        try:
            url = urlsplit(self.path)
            handler = _route(method, url.path, self.server.routes)
            body = self._read_body() if method != "GET" else {}
            if method == "GET":
                status, payload = handler(parse_qs(url.query), body)
            else:
                # Single writer within this service: the lock makes its handlers
                # one-at-a-time, so one claim's peek-then-UPDATE never interleaves
                # with another's, whatever the thread count. Local `sdlc queue`
                # verbs and `sdlc queue run` still write queue.db directly, so
                # claim safety across processes rests on claim_job's guarded
                # UPDATE, not on this lock.
                with self.server.write_lock:
                    status, payload = handler(parse_qs(url.query), body)
        except TimeoutError:
            raise  # a stalled client: http.server logs it and hangs up — not a 500
        except _ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except sqlite3.OperationalError as exc:
            logger.error("queue store busy/unavailable: %s", exc)
            self._send(503, {"error": "queue store unavailable"})
        except Exception:  # noqa: BLE001 — a handler bug must answer 500, not drop the socket
            logger.exception("unhandled error on %s %s", method, _printable(self.path))
            self._send(500, {"error": "internal error"})
        else:
            self._send(status, payload)

    def _browser_refusal(self) -> Decision | None:
        """A refusal when a browser sent the request, else ``None``.

        Whois vouches for the machine, not the program: a browser on an
        allowlisted host lends that identity to every page it renders, and a
        page may POST cross-site with no CORS preflight (a text/plain form, a
        no-cors fetch). Browsers attach ``Origin`` to every POST and DELETE —
        ``null`` when they withhold the page — and a page cannot strip it;
        urllib, curl and the queue client never send it.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return None
        return Decision(False, f"browser-originated request (Origin {origin!r})")

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" and token.strip() else None

    def _read_body(self) -> Body:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise _ApiError(400, "invalid Content-Length") from exc
        if length < 0:
            raise _ApiError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            remaining = min(length, _DRAIN_LIMIT)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise _ApiError(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
        if length == 0:
            return {}
        raw = self.rfile.read(length)  # read before any refusal: the client sees the status
        # No page can send application/json cross-site without a preflight, which
        # this service never answers — so this backs up the Origin check for a
        # client that omits Origin.
        if self.headers.get_content_type() != "application/json":
            raise _ApiError(415, "request body must be Content-Type: application/json")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise _ApiError(400, "request body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _ApiError(400, "request body must be a JSON object")
        return parsed

    def _send(self, status: int, payload: Any) -> None:
        raw = b"" if payload is None else json.dumps(payload, default=str).encode()
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        if status != 204:  # RFC 9110 §8.6: a 204 must not carry Content-Length
            self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)


class _QueueServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: QueueStore,
        policy: AccessPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # The socket family follows the bind address, so a tailnet IPv6 bind works.
        self.address_family = (
            socket.AF_INET6 if ipaddress.ip_address(address[0]).version == 6 else socket.AF_INET
        )
        super().__init__(address, _Handler)
        self.policy = policy
        self.routes = _Routes(store, clock)
        self.write_lock = threading.Lock()


def make_server(
    store: QueueStore,
    policy: AccessPolicy,
    host: str,
    port: int = 8790,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the queue service bound to ``host:port``.

    ``host`` must be a tailnet address or loopback — never a wildcard — and the
    store is created/migrated here so a request never meets a stale schema.
    ``clock`` replaces the wall clock every lease decision reads (tests only).
    """
    _check_bind_host(host)
    store.init()
    return _QueueServer((host, port), store, policy, clock)


def serve(store: QueueStore, policy: AccessPolicy, host: str, port: int) -> None:
    """Run the service until interrupted (Ctrl-C / SIGTERM)."""
    server = make_server(store, policy, host, port)
    bound_host, bound_port = server.server_address[:2]

    def _graceful(*_: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, _graceful)  # so `kill` shuts down cleanly
    except ValueError:
        previous = None  # not the main thread (e.g. under test)
    logger.info("queue service listening on %s:%s (store: %s)", bound_host, bound_port, store.db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)  # hand the caller its handler back
