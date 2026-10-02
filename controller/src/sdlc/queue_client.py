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
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlsplit

import yaml

from sdlc.queue import (
    JobRecord,
    QueueBackend,
    QueueError,
    QueuePause,
    QueueStore,
    WorkerRecord,
    default_queue_path,
)
from sdlc.registry import DASHBOARD_URL_ENV, WORKER_ENV, RunRecord, normalize_dashboard_url

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
    "push_fleet_run",
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
# A run's registry push rides on the worker's own critical path (build start,
# finish), so it gets a short leash: the local file is authoritative anyway.
PUSH_TIMEOUT_SECONDS = 3

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
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # urlsplit's own refusal, e.g. an unclosed `http://[::1`
        raise QueueConfigError(f"{source}: {url!r} is not a valid URL ({exc})") from exc
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


def resolve_queue_url(
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The fleet queue's base URL, or ``None`` for the local SQLite store.

    Precedence is env > file > none. A blank ``SDLC_QUEUE_URL`` counts as unset,
    so ``SDLC_QUEUE_URL= sdlc ...`` is the one-shot way back to the local queue.
    A value that is set but malformed raises :class:`QueueConfigError` rather
    than quietly falling back — a typo must not turn a fleet enqueue into a
    local one.

    ``environ`` resolves for *another process* — a LaunchAgent's own environment,
    say — in which case that process's working directory is unknown and the
    repo-level file is skipped; only its variable and the per-user file count.
    """
    env = (os.environ if environ is None else environ).get(QUEUE_URL_ENV, "").strip()
    if env:
        return _clean_url(env, QUEUE_URL_ENV)
    candidates = [(home or _home()) / USER_CONFIG_FILENAME]
    if environ is None:
        candidates.insert(0, (cwd or Path.cwd()) / QUEUE_CONFIG_FILENAME)
    for path in candidates:
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
    serve` — the scheduler's whole surface included (Story 35.2-005), so a
    worker drains the fleet queue through this class. The service owns the
    store, so :meth:`init` and :meth:`ensure_migrated` are no-ops here, and the
    ``now`` the local store takes is accepted but ignored: the service's clock
    judges every lease, so no worker's skew decides who owns a job. Requests carry
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
        # One retry. A read replays safely after any transport error, and so does
        # a PUT (an idempotent upsert); any other write only when the connection
        # never opened — a reset after the request went out may mean it landed,
        # and replaying `add` would enqueue twice.
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
                replayable = method in ("GET", "PUT", "DELETE") or _failed_before_send(reason)
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

    def register_worker(
        self,
        name: str,
        *,
        host: str,
        pools: Iterable[str] = (),
        harnesses: Iterable[str] = (),
        sandbox: str | None = None,
        repos: Iterable[str] = (),
        slots: int = 1,
        slots_free: int | None = None,
        now: datetime | None = None,
    ) -> WorkerRecord:
        """``POST /workers`` — register this worker, or heartbeat if it already has."""
        body: dict[str, Any] = {
            "worker": name,
            "host": host,
            "pools": list(pools),
            "harnesses": list(harnesses),
            "repos": list(repos),
            "slots": slots,
        }
        if sandbox is not None:
            body["sandbox"] = sandbox
        if slots_free is not None:
            body["slots_free"] = slots_free
        return self._worker(self._call("POST", "/workers", body))

    def list_workers(self) -> list[WorkerRecord]:
        payload = self._call("GET", "/workers")
        if not isinstance(payload, dict) or not isinstance(payload.get("workers"), list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed worker list")
        return [self._worker(item) for item in payload["workers"]]

    def _worker(self, payload: Any) -> WorkerRecord:
        if not isinstance(payload, dict):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed worker record")
        return WorkerRecord(**_fields(WorkerRecord, payload))

    def put_fleet_run(self, record: RunRecord, *, now: datetime | None = None) -> None:
        """``PUT /runs`` — push one run's record to the fleet registry (Story 35.4-001)."""
        self._call("PUT", "/runs", record.to_dict())

    def list_fleet_runs(self) -> list[dict[str, Any]]:
        """``GET /runs`` — every pushed run, each with its worker's ``worker_online`` flag."""
        payload = self._call("GET", "/runs")
        if not isinstance(payload, dict) or not isinstance(payload.get("runs"), list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed run list")
        return [row for row in payload["runs"] if isinstance(row, dict)]

    def renew_lease(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
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

    def release_claim(
        self,
        job_id: int,
        *,
        claimed_by: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
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

    def requeue_job(self, job_id: int, *, now: datetime | None = None) -> None:
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
        pool: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        body: dict[str, Any] = {"until": until.isoformat()}
        optional = {
            "reason": reason, "run_id": run_id, "repo": repo, "source": source, "pool": pool,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        payload = self._call("POST", "/pause", body)
        return bool(payload and payload.get("opened"))

    def dispatch_pause(self, pool: str | None = None) -> QueuePause | None:
        """``pool``'s recorded window — raw, like the store's: an elapsed one is still returned."""
        return next((p for p in self.dispatch_pauses() if p.pool == pool), None)

    def dispatch_pauses(self) -> list[QueuePause]:
        """Every recorded window, one per pool (``GET /pause``).

        Raw on purpose, as the store's is: the scheduler announces a resume off a
        window that has elapsed but is still on record, which the `GET /jobs`
        snapshot (live windows only) cannot show it.
        """
        payload = self._call("GET", "/pause")
        pauses = payload.get("pauses") if isinstance(payload, dict) else None
        if not isinstance(pauses, list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed pause list")
        return [QueuePause(**_fields(QueuePause, p)) for p in pauses if isinstance(p, dict)]

    def mark_pause_probed(self, pool: str | None = None, *, now: datetime | None = None) -> None:
        """``POST /pause/probed`` — stamp ``pool``'s last live-API re-probe."""
        self._call("POST", "/pause/probed", {"pool": pool} if pool else {})

    def clear_pause(self, pool: str | None = None) -> None:
        self._call("DELETE", "/pause" + (f"?pool={quote(pool, safe='')}" if pool else ""))

    # --- the scheduler's surface (Story 35.2-005) -----------------------------

    def _jobs(self, path: str, body: dict[str, Any] | None = None, *, method: str = "GET") -> list[JobRecord]:
        payload = self._call(method, path, body)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed job list")
        return [self._job(item) for item in payload["jobs"]]

    def peek_claimable(
        self,
        *,
        busy_repos: "set[str] | frozenset[str] | None" = None,
        fix_busy_repos: "set[str] | frozenset[str] | None" = None,
        now: datetime | None = None,
    ) -> list[JobRecord]:
        """``GET /jobs/claimable`` — candidates in dispatch order, minus the busy repos."""
        params = [("busy", repo) for repo in sorted(busy_repos or ())]
        params += [("fix_busy", repo) for repo in sorted(fix_busy_repos or ())]
        return self._jobs("/jobs/claimable" + (f"?{urlencode(params)}" if params else ""))

    def claimable_for_worker(
        self,
        name: str,
        candidates: Iterable[JobRecord],
        *,
        slots_free: int | None = None,
        now: datetime | None = None,
    ) -> list[JobRecord]:
        """``POST /jobs/eligible`` — which candidates ``name`` may take, order kept.

        Sends job ids, not records: the service re-reads each, so it judges the
        row as it is now rather than as this worker last saw it.
        """
        body: dict[str, Any] = {"worker": name, "job_ids": [job.id for job in candidates]}
        if slots_free is not None:
            body["slots_free"] = slots_free
        return self._jobs("/jobs/eligible", body, method="POST")

    def expired_running_jobs(self, *, now: datetime | None = None) -> list[JobRecord]:
        return self._jobs("/jobs/expired")

    def due_parked_jobs(self, *, now: datetime | None = None) -> list[JobRecord]:
        return self._jobs("/jobs/due")

    def running_repos(
        self, *, kind: str | None = None, excluding: int | None = None
    ) -> set[str]:
        params = {key: value for key, value in (("kind", kind), ("excluding", excluding)) if value is not None}
        payload = self._call("GET", "/jobs/running-repos" + (f"?{urlencode(params)}" if params else ""))
        repos = payload.get("repos") if isinstance(payload, dict) else None
        if not isinstance(repos, list):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed repo list")
        return {repo for repo in repos if isinstance(repo, str)}

    def overlap_holds(self) -> dict[int, int]:
        payload = self._call("GET", "/jobs/holds")
        holds = payload.get("holds") if isinstance(payload, dict) else None
        if not isinstance(holds, dict):
            raise QueueUnavailable(f"fleet queue {self.url} sent a malformed overlap graph")
        return {int(job): int(holder) for job, holder in holds.items()}

    def get_worker(self, name: str) -> WorkerRecord | None:
        try:
            return self._worker(self._call("GET", f"/workers/{quote(name, safe='')}"))
        except QueueRequestError as exc:
            if exc.status == 404:
                return None
            raise

    def _take(self, verb: str, job_id: int, claimed_by: str, lease_seconds: int, worker: str | None) -> JobRecord | None:
        """The three lease-taking verbs: a lost race (409) or a vanished job (404) is ``None``."""
        body: dict[str, Any] = {"claimed_by": claimed_by, "lease_seconds": lease_seconds}
        if worker is not None:
            body["worker"] = worker
        try:
            return self._job(self._call("POST", f"/jobs/{job_id}/{verb}", body))
        except QueueRequestError as exc:
            if exc.status in (404, 409):
                return None
            raise

    def claim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
        worker: str | None = None,
    ) -> JobRecord | None:
        return self._take("claim", job_id, claimed_by, lease_seconds, worker)

    def reclaim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
        worker: str | None = None,
    ) -> JobRecord | None:
        return self._take("reclaim", job_id, claimed_by, lease_seconds, worker)

    def take_parked_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
        worker: str | None = None,
    ) -> JobRecord | None:
        return self._take("take", job_id, claimed_by, lease_seconds, worker)

    def park_job(
        self, job_id: int, *, pr_number: int, reason: str, poll_after: datetime | None
    ) -> bool:
        payload = self._call(
            "POST",
            f"/jobs/{job_id}/park",
            {
                "pr_number": pr_number,
                "reason": reason,
                "poll_after": poll_after.isoformat() if poll_after is not None else None,
            },
        )
        return bool(isinstance(payload, dict) and payload.get("parked"))

    def schedule_poll(self, job_id: int, poll_after: datetime | None) -> None:
        self._call(
            "POST",
            f"/jobs/{job_id}/poll",
            {"poll_after": poll_after.isoformat() if poll_after is not None else None},
        )

    def set_reason(self, job_id: int, reason: str | None) -> None:
        self._call("POST", f"/jobs/{job_id}/reason", {"reason": reason})

    def attach_run(self, job_id: int, run_id: str) -> None:
        self._call("POST", f"/jobs/{job_id}/run", {"run_id": run_id})

    def record_sync(self, job_id: int, *, repo: str, sha: str) -> None:
        self._call("POST", f"/jobs/{job_id}/sync", {"repo": repo, "sha": sha})

    def record_files(self, job_id: int, paths: Iterable[str]) -> None:
        self._call("POST", f"/jobs/{job_id}/files", {"paths": sorted({str(p) for p in paths})})

    def record_fix_rounds_baseline(self, job_id: int, rounds: int) -> None:
        self._call("POST", f"/jobs/{job_id}/fix-rounds", {"rounds": rounds})

    def restart_fresh(self, job_id: int, *, reason: str, now: datetime | None = None) -> None:
        self._call("POST", f"/jobs/{job_id}/restart", {"reason": reason})


# ---------------------------------------------------------------------------
# Fleet registry push (Story 35.4-001)
# ---------------------------------------------------------------------------


def fleet_worker_name() -> str:
    """Who this process pushes runs as: ``SDLC_WORKER``, else the short hostname."""
    return os.environ.get(WORKER_ENV, "").strip() or socket.gethostname().split(".")[0]


def _advertised_dashboard_url() -> str | None:
    """``SDLC_DASHBOARD_URL`` as an origin; unset or malformed advertises nothing."""
    try:
        return normalize_dashboard_url(os.environ[DASHBOARD_URL_ENV])
    except (KeyError, ValueError):
        return None


def push_fleet_run(record: RunRecord) -> None:
    """Best-effort ``PUT /runs`` of ``record``; a no-op with no fleet configured.

    The worker's local registry file stays authoritative, so no failure here —
    a down service, a refused identity, a malformed ``SDLC_QUEUE_URL`` — may
    reach the build that called it: the fleet view just stays a push behind.
    """
    try:
        url = resolve_queue_url()
        if url is None:
            return
        client = QueueClient(
            url, token=os.environ.get(QUEUE_TOKEN_ENV) or None, timeout=PUSH_TIMEOUT_SECONDS
        )
        client.put_fleet_run(
            dataclasses.replace(
                record,
                worker=record.worker or fleet_worker_name(),
                dashboard_url=record.dashboard_url or _advertised_dashboard_url(),
            )
        )
    except (QueueError, OSError, ValueError):
        pass
