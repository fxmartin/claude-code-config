# ABOUTME: QueueClient — the QueueBackend verbs over the `sdlc queue serve` HTTP API.
# ABOUTME: Story 35.1-002. Also the SDLC_QUEUE_URL switch and the open_queue() factory.

from __future__ import annotations

import dataclasses
import errno
import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import yaml

from sdlc.queue import JobRecord, QueueBackend, QueueError, QueuePause, QueueStore, default_queue_path

__all__ = [
    "QUEUE_CONFIG_FILENAME",
    "QUEUE_TOKEN_ENV",
    "QUEUE_URL_ENV",
    "USER_CONFIG_FILENAME",
    "QueueClient",
    "QueueConfigError",
    "QueueRefused",
    "QueueRequestError",
    "QueueUnavailable",
    "open_queue",
    "resolve_queue_url",
]

QUEUE_URL_ENV = "SDLC_QUEUE_URL"
# The same shared secret `sdlc queue serve` reads: one variable on both ends.
QUEUE_TOKEN_ENV = "SDLC_QUEUE_TOKEN"
# The file form of SDLC_QUEUE_URL, mirroring `.sdlc-forge.yaml`: a repo-level
# `.sdlc-queue.yaml` (cwd) is the more specific, so it outranks the per-user
# `~/.sdlc-fleet.yaml`. Both are flat: `queue_url: http://host:8790`.
QUEUE_CONFIG_FILENAME = ".sdlc-queue.yaml"
USER_CONFIG_FILENAME = ".sdlc-fleet.yaml"

REQUEST_TIMEOUT_SECONDS = 10

_Opener = Callable[[urllib.request.Request, float], Any]


class QueueConfigError(QueueError):
    """``SDLC_QUEUE_URL`` or a fleet config file is present but unusable."""


class QueueUnavailable(QueueError):
    """The fleet queue could not be reached, or answered with a server error."""


class QueueRefused(QueueError):
    """The service answered 403: this caller's identity is not accepted."""


class QueueRequestError(QueueError):
    """The service understood the request and said no (400/404/409): ``status`` says which."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Configuration: SDLC_QUEUE_URL (env) > .sdlc-queue.yaml / ~/.sdlc-fleet.yaml > none
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def _clean_url(raw: object, source: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise QueueConfigError(f"{source}: queue_url must be a non-empty URL")
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise QueueConfigError(
            f"{source}: {url!r} is not an http(s) URL (e.g. http://home-lab.<tailnet>:8790)"
        )
    return url


def _url_from_file(path: Path) -> str | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise QueueConfigError(f"{path.name} could not be read: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise QueueConfigError(f"{path.name} must be a mapping")
    if "queue_url" not in raw:
        return None
    return _clean_url(raw["queue_url"], path.name)


def resolve_queue_url(*, cwd: Path | None = None, home: Path | None = None) -> str | None:
    """The fleet queue's base URL, or ``None`` for the local SQLite store.

    Precedence is env > file > none. A blank ``SDLC_QUEUE_URL`` counts as unset,
    so ``SDLC_QUEUE_URL= sdlc ...`` is the one-shot way back to the local queue.
    A value that is set but malformed raises :class:`QueueConfigError` rather
    than quietly falling back — a typo must not turn a fleet enqueue into a
    local one.
    """
    env = os.environ.get(QUEUE_URL_ENV, "").strip()
    if env:
        return _clean_url(env, QUEUE_URL_ENV)
    for path in ((cwd or Path.cwd()) / QUEUE_CONFIG_FILENAME, (home or _home()) / USER_CONFIG_FILENAME):
        if path.is_file():
            url = _url_from_file(path)
            if url is not None:
                return url
    return None


def open_queue() -> QueueBackend:
    """The queue every consumer opens: the fleet's when configured, else local SQLite.

    Opening is lazy — a :class:`QueueClient` makes no request until a verb is
    called, so a plain command never pays for (or fails on) a service it does
    not use.
    """
    url = resolve_queue_url()
    if url is None:
        return QueueStore(default_queue_path())
    return QueueClient(url, token=os.environ.get(QUEUE_TOKEN_ENV) or None)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

# Direct, never through $http_proxy: the service is a tailnet peer and a proxy
# from the environment would only add a way to fail.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return _DIRECT.open(req, timeout=timeout)


def _fields(cls: type, payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` restricted to ``cls``'s fields, so a newer service's extra keys are ignored."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in payload.items() if k in names}


def _failed_before_send(reason: object) -> bool:
    """Whether the connection never got far enough to carry the request."""
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    return isinstance(reason, OSError) and reason.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)


class QueueClient:
    """:class:`~sdlc.queue.QueueBackend` over HTTP — the fleet queue's remote face.

    Each verb is one request to the route of the same name on `sdlc queue
    serve`. The service owns the store, so :meth:`init` and
    :meth:`ensure_migrated` are no-ops here. Requests carry
    ``Authorization: Bearer <token>`` when a token is configured (otherwise the
    service identifies this host through Tailscale).

    Failure contract: a transport problem or a 5xx raises
    :class:`QueueUnavailable`, a 403 :class:`QueueRefused`; both name the URL.
    Verbs that return ``bool`` or ``None`` on the local store keep doing so for
    "not yours / not found" answers, so callers cannot tell the backends apart.
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        opener: _Opener | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener: _Opener = opener or _default_opener

    # --- transport ------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        # One retry. A read replays safely after any transport error; a write
        # only when the connection never opened — a reset after the request
        # went out may mean it landed, and replaying `add` would enqueue twice.
        for attempt in (1, 2):
            req = urllib.request.Request(self.url + path, data=data, method=method)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if self._token:
                req.add_header("Authorization", f"Bearer {self._token}")
            try:
                with self._opener(req, self._timeout) as resp:
                    return resp.status, self._decode(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, self._decode(self._body_of(exc))
            except (OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                replayable = method in ("GET", "DELETE") or _failed_before_send(reason)
                if attempt == 1 and replayable:
                    continue
                raise QueueUnavailable(f"fleet queue {self.url} unreachable: {reason}") from exc
        raise AssertionError("unreachable")  # pragma: no cover — the loop always returns or raises

    @staticmethod
    def _body_of(exc: urllib.error.HTTPError) -> bytes:
        try:
            return exc.read()
        except Exception:  # noqa: BLE001 — an unreadable error body is just an empty one
            return b""

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": raw[:200].decode(errors="replace")}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, payload = self._request(method, path, body)
        if 200 <= status < 300:
            return payload
        message = payload.get("error") if isinstance(payload, dict) else None
        if status == 403:
            raise QueueRefused(
                f"fleet queue {self.url} refused this caller (403): identity not accepted"
            )
        if status >= 500 or not isinstance(message, str):
            raise QueueUnavailable(
                f"fleet queue {self.url} answered {status}"
                + (f": {message}" if isinstance(message, str) else "")
            )
        raise QueueRequestError(status, message)

    def _job(self, payload: Any) -> JobRecord:
        if not isinstance(payload, dict):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed job record")
        return JobRecord(**_fields(JobRecord, payload))

    def _snapshot(self, repo: str | None = None) -> dict[str, Any]:
        path = "/jobs" + (f"?repo={quote(repo, safe='')}" if repo is not None else "")
        payload = self._call("GET", path)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed job list")
        return payload

    # --- QueueBackend ---------------------------------------------------

    def init(self) -> None:
        """No-op: the service creates and migrates its own store."""

    def ensure_migrated(self) -> None:
        """No-op: see :meth:`init`."""

    def health(self) -> dict[str, Any]:
        """``GET /health`` — raises unless the service is up and admits this caller."""
        payload = self._call("GET", "/health")
        return payload if isinstance(payload, dict) else {}

    def add_job(
        self,
        *,
        repo: str,
        kind: str,
        scope: str,
        priority: str | None = None,
        options_json: str | None = None,
        labels: Iterable[str] = (),
        host: str | None = None,
        pool: str | None = None,
        requirements_json: str | None = None,
    ) -> int:
        body: dict[str, Any] = {"repo": repo, "kind": kind, "scope": scope}
        optional: dict[str, Any] = {
            "priority": priority,
            "options": self._json_field("options_json", options_json),
            "labels": list(labels) or None,
            "host": host,
            "pool": pool,
            "requirements": self._json_field("requirements_json", requirements_json),
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._job(self._call("POST", "/jobs", body)).id

    @staticmethod
    def _json_field(name: str, raw: str | None) -> Any:
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise QueueError(f"{name} is not valid JSON: {exc}") from exc

    def get_job(self, job_id: int) -> JobRecord | None:
        # The service has no single-job read; the list is small and this is rare.
        for job in self.list_jobs():
            if job.id == job_id:
                return job
        return None

    def list_jobs(self, repo: str | None = None) -> list[JobRecord]:
        return [self._job(item) for item in self._snapshot(repo)["jobs"]]

    def claim_next(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        host: str | None = None,
        pools: Iterable[str] | None = None,
    ) -> JobRecord | None:
        body: dict[str, Any] = {"worker": claimed_by, "lease_seconds": lease_seconds}
        if host is not None:
            body["host"] = host
        if pools is not None:
            body["pools"] = list(pools)
        payload = self._call("POST", "/jobs/claim", body)
        return self._job(payload) if payload is not None else None

    def renew_lease(self, job_id: int, *, claimed_by: str, lease_seconds: int) -> bool:
        try:
            self._call(
                "POST",
                f"/jobs/{job_id}/renew",
                {"worker": claimed_by, "lease_seconds": lease_seconds},
            )
        except QueueRequestError as exc:
            if exc.status in (404, 409):
                return False
            raise
        return True

    def release_claim(self, job_id: int, *, claimed_by: str, reason: str | None = None) -> None:
        body: dict[str, Any] = {"worker": claimed_by}
        if reason is not None:
            body["reason"] = reason
        try:
            self._call("POST", f"/jobs/{job_id}/release", body)
        except QueueRequestError as exc:
            if exc.status not in (404, 409):  # not ours to release: silent, like the store
                raise

    def finish_job(
        self,
        job_id: int,
        state: str,
        *,
        reason: str | None = None,
        claimed_by: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {"state": state}
        if reason is not None:
            body["reason"] = reason
        if claimed_by is not None:
            body["worker"] = claimed_by
        try:
            self._call("POST", f"/jobs/{job_id}/finish", body)
        except QueueRequestError as exc:
            if exc.status in (404, 409):
                return False
            raise
        return True

    def cancel_job(self, job_id: int) -> None:
        self._call("POST", f"/jobs/{job_id}/cancel")

    def requeue_job(self, job_id: int) -> None:
        self._call("POST", f"/jobs/{job_id}/requeue")

    def prioritise_job(self, job_id: int, priority_class: str) -> None:
        self._call("POST", f"/jobs/{job_id}/prioritise", {"priority": priority_class})

    def pause_dispatch(
        self,
        *,
        until: datetime,
        reason: str | None = None,
        run_id: str | None = None,
        repo: str | None = None,
        source: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {"until": until.isoformat()}
        optional = {"reason": reason, "run_id": run_id, "repo": repo, "source": source}
        body.update({key: value for key, value in optional.items() if value is not None})
        payload = self._call("POST", "/pause", body)
        return bool(payload and payload.get("opened"))

    def dispatch_pause(self) -> QueuePause | None:
        """The live window; the service already reports an elapsed one as none."""
        pause = self._snapshot().get("pause")
        return QueuePause(**_fields(QueuePause, pause)) if isinstance(pause, dict) else None

    def clear_pause(self) -> None:
        self._call("DELETE", "/pause")
