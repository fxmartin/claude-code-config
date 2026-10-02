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
from urllib.parse import parse_qs, urlsplit

from sdlc.queue import JobRecord, QueueError, QueueStore
from sdlc.scheduler import DEFAULT_LEASE_SECONDS

__all__ = [
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

# The service may also *bind* loopback — unreachable from the tailnet, so no
# exposure, and what makes local development and tests possible. It is never a
# valid *peer* unless the policy is built to trust it explicitly.
_BIND_NETWORKS = TAILNET_NETWORKS + ("127.0.0.0/8", "::1/128")

# Requests are small JSON documents; a megabyte is generous and bounds what an
# authorised-but-buggy client can make a handler thread buffer.
MAX_BODY_BYTES = 1024 * 1024
# How much of an oversized body is read-and-discarded before answering 413, so
# the client sees the status rather than a reset connection.
_DRAIN_LIMIT = 16 * 1024 * 1024

WHOIS_CACHE_SECONDS = 60
_WHOIS_TIMEOUT_SECONDS = 5
_MAX_LEASE_SECONDS = 24 * 3600
_REQUIREMENT_KEYS = frozenset({"repo", "harness", "sandbox"})


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
        if self._token and bearer is not None and hmac.compare_digest(bearer, self._token):
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

    def __init__(self, store: QueueStore) -> None:
        self.store = store

    # --- reads ---------------------------------------------------------

    def list_jobs(self, query: dict[str, list[str]], _body: Body) -> Reply:
        # Same envelope `sdlc queue list --json` emits: the pause lives beside
        # the jobs, not in one of them, and an elapsed window is not state.
        repo = query.get("repo", [None])[0]
        pause = self.store.dispatch_pause()
        if pause is not None and not pause.is_active():
            pause = None
        jobs = self.store.list_jobs(repo)
        return 200, {
            "pause": pause.to_dict() if pause else None,
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
        return 201, _job(self.store, job_id).to_dict()

    def claim(self, _query: Any, body: Body) -> Reply:
        claimed = self.store.claim_next(
            claimed_by=_required(body, "worker"),
            lease_seconds=_lease(body),
            host=_text(body, "host"),
            pools=_string_list(body, "pools"),
        )
        return (200, claimed.to_dict()) if claimed is not None else (204, None)

    def renew(self, job_id: int, body: Body) -> Reply:
        worker = _required(body, "worker")
        job = _job(self.store, job_id)
        if not self.store.renew_lease(job_id, claimed_by=worker, lease_seconds=_lease(body)):
            raise _ApiError(409, f"job {job.id} is not claimed by {worker!r}")
        return 200, _job(self.store, job_id).to_dict()

    def release(self, job_id: int, body: Body) -> Reply:
        worker = _required(body, "worker")
        _held_by(_job(self.store, job_id), worker)
        self.store.release_claim(job_id, claimed_by=worker, reason=_text(body, "reason"))
        return 200, _job(self.store, job_id).to_dict()

    def finish(self, job_id: int, body: Body) -> Reply:
        job = _job(self.store, job_id)
        state = _required(body, "state")
        worker = _text(body, "worker")
        if worker is not None:
            _held_by(job, worker)
        try:
            self.store.finish_job(job_id, state, reason=_text(body, "reason"))
        except QueueError as exc:
            raise _ApiError(400, str(exc)) from exc
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
            self.store.requeue_job(job_id)
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

    # --- the shared rate-limit pause -----------------------------------

    def pause(self, _query: Any, body: Body) -> Reply:
        raw = _required(body, "until")
        try:
            until = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise _ApiError(400, f"until must be an ISO-8601 timestamp, got {raw!r}") from exc
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        opened = self.store.pause_dispatch(
            until=until,
            reason=_text(body, "reason"),
            run_id=_text(body, "run_id"),
            repo=_text(body, "repo"),
            source=_text(body, "source"),
        )
        recorded = self.store.dispatch_pause()
        return 200, {"opened": opened, "pause": recorded.to_dict() if recorded else None}

    def resume(self, _query: Any, _body: Body) -> Reply:
        self.store.clear_pause()
        return 200, {"pause": None}


# The `/jobs/{id}/<verb>` endpoints; each names the _Routes method it maps onto.
_JOB_VERBS = ("renew", "release", "finish", "cancel", "requeue", "prioritise")


def _route(method: str, path: str, routes: _Routes) -> Callable[[dict[str, list[str]], Body], Reply]:
    parts = [p for p in path.split("/") if p]
    table: dict[tuple[str, tuple[str, ...]], Callable[[dict[str, list[str]], Body], Reply]] = {
        ("GET", ("jobs",)): routes.list_jobs,
        ("POST", ("jobs",)): routes.add_job,
        ("POST", ("jobs", "claim")): routes.claim,
        ("POST", ("pause",)): routes.pause,
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
        verb = getattr(routes, parts[2])
        return lambda _query, body: verb(job_id, body)
    if any(key[1] == tuple(parts) for key in table):
        raise _ApiError(405, f"{method} not allowed on {path}")
    raise _ApiError(404, f"no such route: {path}")


class _Handler(BaseHTTPRequestHandler):
    server: "_QueueServer"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s - %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        peer = self.client_address[0]
        decision = self.server.policy.authorize(peer, self._bearer())
        if not decision.allowed:
            logger.warning("refused %s %s from %s: %s", method, self.path, peer, decision.detail)
            self._send(403, {"error": "forbidden"})
            return
        try:
            url = urlsplit(self.path)
            handler = _route(method, url.path, self.server.routes)
            body = self._read_body() if method != "GET" else {}
            if method == "GET":
                status, payload = handler(parse_qs(url.query), body)
            else:
                # Single writer by construction: every mutation in the fleet
                # funnels through this one process, and this lock makes the
                # funnel one-at-a-time — a claim's peek-then-UPDATE never
                # interleaves with another writer's, whatever the thread count.
                with self.server.write_lock:
                    status, payload = handler(parse_qs(url.query), body)
        except _ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except sqlite3.OperationalError as exc:
            logger.error("queue store busy/unavailable: %s", exc)
            self._send(503, {"error": "queue store unavailable"})
        except Exception:  # noqa: BLE001 — a handler bug must answer 500, not drop the socket
            logger.exception("unhandled error on %s %s", method, self.path)
            self._send(500, {"error": "internal error"})
        else:
            self._send(status, payload)

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
        try:
            parsed = json.loads(self.rfile.read(length))
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
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)


class _QueueServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: QueueStore, policy: AccessPolicy) -> None:
        # The socket family follows the bind address, so a tailnet IPv6 bind works.
        self.address_family = (
            socket.AF_INET6 if ipaddress.ip_address(address[0]).version == 6 else socket.AF_INET
        )
        super().__init__(address, _Handler)
        self.policy = policy
        self.routes = _Routes(store)
        self.write_lock = threading.Lock()


def make_server(
    store: QueueStore,
    policy: AccessPolicy,
    host: str,
    port: int = 8790,
) -> ThreadingHTTPServer:
    """Build (but do not start) the queue service bound to ``host:port``.

    ``host`` must be a tailnet address or loopback — never a wildcard — and the
    store is created/migrated here so a request never meets a stale schema.
    """
    _check_bind_host(host)
    store.init()
    return _QueueServer((host, port), store, policy)


def serve(store: QueueStore, policy: AccessPolicy, host: str, port: int) -> None:
    """Run the service until interrupted (Ctrl-C / SIGTERM)."""
    server = make_server(store, policy, host, port)
    bound_host, bound_port = server.server_address[:2]

    def _graceful(*_: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _graceful)  # so `kill` shuts down cleanly
    except ValueError:
        pass  # not the main thread (e.g. under test)
    logger.info("queue service listening on %s:%s (store: %s)", bound_host, bound_port, store.db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
