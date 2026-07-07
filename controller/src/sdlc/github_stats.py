# ABOUTME: Forge-agnostic repo-health fetch for the dashboard — issue/CR counts + default-branch CI.
# ABOUTME: Story 11.2-006 (GitHub) + 23.7-001 (GitLab) — per-(host,slug) TTL cache, graceful "unavailable".

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from sdlc.issue_host import GITHUB, GITLAB

__all__ = [
    "GITHUB",
    "GITLAB",
    "GitHubStatsCache",
    "fetch_stats",
    "fetch_gitlab_stats",
    "normalize_ci",
    "normalize_gitlab_ci",
    "parse_ci_runs",
    "unavailable",
]

# `gh`/`glab` talk to the forge over the network; cap each call so a hung
# request can never wedge the background refresh. This budget is the *fetch*
# path's, never the dashboard request path's — the cache always serves the
# request.
_GH_TIMEOUT = 10.0

# Server-side refresh cadence. The browser's ~2.5 s poll reads whatever the
# cache last produced; only a stale entry triggers a (background) `gh` call,
# so GitHub's rate limit is respected no matter how many tabs/runs are open.
_DEFAULT_TTL = 60.0


# --- gh invocation ----------------------------------------------------------


def _run_gh(args: list[str], timeout: float = _GH_TIMEOUT) -> str | None:
    """Run ``gh ARGS`` and return stdout text, or None on any failure.

    Returns None when ``gh`` is absent, errors, times out, or exits non-zero
    (unauthenticated / rate-limited / no such repo) — the caller then degrades
    to the "unavailable" sentinel rather than raising.
    """
    try:
        out = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _run_glab(args: list[str], timeout: float = _GH_TIMEOUT) -> str | None:
    """Run ``glab ARGS`` and return stdout text, or None on any failure.

    The GitLab twin of :func:`_run_gh`: returns None when ``glab`` is absent,
    errors, times out, or exits non-zero (unauthenticated / no such project) so
    the caller degrades to the "unavailable" sentinel rather than raising.
    """
    try:
        out = subprocess.run(
            ["glab", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


# --- pure parsing -----------------------------------------------------------


def normalize_ci(run: dict | None) -> dict | None:
    """Collapse a ``gh run`` row into the dashboard's CI shape, or None.

    GitHub splits a run's state across ``status`` (queued/in_progress/completed)
    and ``conclusion`` (success/failure/cancelled/…). A run that has not yet
    completed has no conclusion, so it reads as ``in_progress``; a completed run
    reports its conclusion verbatim (``unknown`` if absent, never crashing).
    """
    if run is None:
        return None
    status = run.get("status")
    conclusion = run.get("conclusion")
    ci_status = (conclusion or "unknown") if status == "completed" else "in_progress"
    return {
        "status": ci_status,
        "branch": run.get("headBranch"),
        "created_at": run.get("createdAt"),
    }


def parse_ci_runs(stdout: str | None) -> dict | None:
    """Parse ``gh run list --json`` output and normalize the newest run, or None.

    ``gh run list --limit 1`` returns a JSON array (newest first); we take its
    head. Malformed/empty output yields None so CI simply shows "—".
    """
    if not stdout:
        return None
    try:
        runs = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(runs, list) or not runs:
        return None
    return normalize_ci(runs[0])


def _count(stdout: str | None) -> int | None:
    """Parse a search ``total_count`` line into an int, or None on failure."""
    if stdout is None:
        return None
    try:
        return int(stdout.strip())
    except (ValueError, AttributeError):
        return None


def _json_len(stdout: str | None) -> int | None:
    """Count rows in a ``glab … list --output json`` array, or None on bad output.

    GitLab has no per-search ``total_count`` endpoint the way GitHub's search API
    does, so the count is the length of the listed page (a health-at-a-glance
    signal, capped by ``glab``'s default page size). Malformed/absent output
    yields None so the field renders as "—" rather than crashing.
    """
    if stdout is None:
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return len(data) if isinstance(data, list) else None


# GitLab pipeline `status` → the dashboard's CI vocabulary (the same tokens the
# GitHub path emits, so the client glyph map is shared). Anything unrecognised
# reads as ``unknown`` (renders as "—"); a pipeline with no status carries no CI
# signal (see :func:`normalize_gitlab_ci`).
_GITLAB_CI_STATUS = {
    "success": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "running": "in_progress",
    "pending": "in_progress",
    "created": "in_progress",
    "preparing": "in_progress",
    "waiting_for_resource": "in_progress",
    "scheduled": "in_progress",
    "manual": "in_progress",
    "skipped": "unknown",
}


def normalize_gitlab_ci(pipeline: dict | None) -> dict | None:
    """Collapse a GitLab pipeline object into the dashboard's CI shape, or None.

    The GitLab twin of :func:`normalize_ci`: maps the pipeline ``status`` onto
    the shared CI vocabulary and carries the branch (``ref``) and ``created_at``.
    Returns None when there is no pipeline or it reports no status, so CI shows
    "—" instead of throwing.
    """
    if not isinstance(pipeline, dict):
        return None
    raw = pipeline.get("status")
    if not raw:
        return None
    return {
        "status": _GITLAB_CI_STATUS.get(str(raw).lower(), "unknown"),
        "branch": pipeline.get("ref"),
        "created_at": pipeline.get("created_at"),
    }


# --- fetch ------------------------------------------------------------------


def unavailable(slug: str | None, reason: str, host: str | None = None) -> dict:
    """The muted sentinel rendered when repo-health data cannot be obtained.

    Every count/CI field is present and null so the client renders a uniform
    "unavailable" state and never throws on a missing key. ``host`` names the
    forge (``github``/``gitlab``) so the client can pick host-appropriate wording
    ("GitLab unavailable" vs "GitHub unavailable"); None when unknown.
    """
    return {
        "available": False,
        "slug": slug,
        "host": host,
        "reason": reason,
        "issues_open": None,
        "issues_closed": None,
        "prs_open": None,
        "prs_closed": None,
        "ci_status": None,
        "ci_branch": None,
        "ci_created_at": None,
    }


def _search_count(slug: str, qualifier: str) -> int | None:
    """Issue/PR count via the search API ``total_count`` for ``repo:SLUG QUAL``."""
    return _count(
        _run_gh(
            ["api", "-X", "GET", "search/issues",
             "-f", f"q=repo:{slug} {qualifier}", "--jq", ".total_count"]
        )
    )


def _default_branch(slug: str) -> str | None:
    out = _run_gh(["api", f"repos/{slug}", "--jq", ".default_branch"])
    if out is None:
        return None
    branch = out.strip()
    return branch or None


def _fetch_ci(slug: str) -> dict | None:
    """Latest workflow run on the repo's *default* branch, normalized, or None."""
    branch = _default_branch(slug)
    if branch is None:
        return None
    return parse_ci_runs(
        _run_gh(
            ["run", "list", "--repo", slug, "--branch", branch, "--limit", "1",
             "--json", "status,conclusion,headBranch,createdAt"]
        )
    )


def fetch_stats(slug: str | None, host: str = GITHUB) -> dict:
    """Fetch one repo's health (issues/CRs/CI), routed by ``host`` (Story 23.7-001).

    Dispatches to the GitHub (``gh``) or GitLab (``glab``) fetcher behind one
    stats shape, so a GitLab project shows GitLab health instead of the "GitHub
    unavailable" sentinel. ``host`` defaults to GitHub, keeping every pre-existing
    call byte-identical. A missing slug degrades to the ``no-remote`` sentinel.
    """
    if not slug:
        return unavailable(None, "no-remote", host=host)
    if host == GITLAB:
        return fetch_gitlab_stats(slug)
    return _fetch_github_stats(slug)


def _fetch_github_stats(slug: str) -> dict:
    """Fetch one repo's GitHub health (issues/PRs/CI), keyed by ``owner/repo``.

    Closed-PR count includes merged PRs (GitHub counts a merged PR as closed).
    When *every* underlying call fails the repo is reported unavailable
    (``gh`` missing / unauthenticated / rate-limited); a partial result (e.g.
    counts present but no CI runs yet) still renders as available.
    """
    counts = {
        "issues_open": _search_count(slug, "type:issue state:open"),
        "issues_closed": _search_count(slug, "type:issue state:closed"),
        "prs_open": _search_count(slug, "type:pr state:open"),
        "prs_closed": _search_count(slug, "type:pr state:closed"),
    }
    ci = _fetch_ci(slug)
    if ci is None and all(v is None for v in counts.values()):
        return unavailable(slug, "gh-unavailable", host=GITHUB)
    return {
        "available": True,
        "slug": slug,
        "host": GITHUB,
        "reason": None,
        **counts,
        "ci_status": ci["status"] if ci else None,
        "ci_branch": ci["branch"] if ci else None,
        "ci_created_at": ci["created_at"] if ci else None,
    }


# --- GitLab fetch (Story 23.7-001) ------------------------------------------
# `glab` is targeted by slug (`-R owner/repo`, or the URL-encoded project path in
# the REST endpoint) rather than the process cwd, so the dashboard fetches health
# for the *selected run's* repo — not whatever repo it happens to run in.


def _glab_count(args: list[str]) -> int | None:
    """Count the rows a ``glab … list --output json`` call returns, or None."""
    return _json_len(_run_glab(args))


def _gitlab_default_branch(slug: str) -> str | None:
    """The project's default branch via the REST API, or None on any failure."""
    enc = urllib.parse.quote(slug, safe="")
    out = _run_glab(["api", f"projects/{enc}"])
    if not out:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    branch = data.get("default_branch") if isinstance(data, dict) else None
    return branch or None


def _fetch_gitlab_ci(slug: str) -> dict | None:
    """Latest pipeline on the project's *default* branch, normalized, or None."""
    branch = _gitlab_default_branch(slug)
    if branch is None:
        return None
    enc = urllib.parse.quote(slug, safe="")
    out = _run_glab(["api", f"projects/{enc}/pipelines?ref={branch}&per_page=1"])
    if not out:
        return None
    try:
        pipelines = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(pipelines, list) or not pipelines:
        return None
    return normalize_gitlab_ci(pipelines[0])


def fetch_gitlab_stats(slug: str) -> dict:
    """Fetch one repo's GitLab health (open/closed issues + MRs, default-branch CI).

    MRs map onto the shared ``prs_*`` fields so the dashboard renders them the
    same way it renders GitHub PRs. When *every* underlying call fails the repo
    is reported unavailable (``glab`` missing / unauthenticated / no such
    project); a partial result (counts present but no pipeline yet) still renders
    as available.
    """
    counts = {
        "issues_open": _glab_count(["issue", "list", "-R", slug, "--output", "json"]),
        "issues_closed": _glab_count(
            ["issue", "list", "-R", slug, "--closed", "--output", "json"]
        ),
        "prs_open": _glab_count(["mr", "list", "-R", slug, "--output", "json"]),
        "prs_closed": _glab_count(
            ["mr", "list", "-R", slug, "--closed", "--output", "json"]
        ),
    }
    ci = _fetch_gitlab_ci(slug)
    if ci is None and all(v is None for v in counts.values()):
        return unavailable(slug, "glab-unavailable", host=GITLAB)
    return {
        "available": True,
        "slug": slug,
        "host": GITLAB,
        "reason": None,
        **counts,
        "ci_status": ci["status"] if ci else None,
        "ci_branch": ci["branch"] if ci else None,
        "ci_created_at": ci["created_at"] if ci else None,
    }


# --- per-slug TTL cache, off the request path -------------------------------


def _spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


@dataclass
class _Entry:
    stats: dict
    fetched_at: float


class GitHubStatsCache:
    """Serve repo-health stats from a per-(host,slug) cache, off the request path.

    A ``get(slug, host)`` never blocks on ``gh``/``glab``: a fresh entry is
    returned as-is; a stale/absent one returns the last-known value (or a pending
    sentinel) and schedules a background refresh. Refreshes are deduped per
    (host, slug), so N runs in one repo — and any number of polling tabs — cost a
    single fetch per TTL window, keeping the forge's rate limit safe. The key
    carries the host because the same ``owner/repo`` slug is distinct data on
    each forge. ``fetcher``/``clock``/``spawn`` are injectable so the cache is
    testable without threads or a live CLI. (The class name is retained to avoid
    churning importers; the surface is forge-agnostic — Story 23.7-001.)
    """

    def __init__(
        self,
        *,
        ttl: float = _DEFAULT_TTL,
        fetcher: Callable[[str, str], dict] | None = None,
        clock: Callable[[], float] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._ttl = ttl
        self._fetcher = fetcher or fetch_stats
        self._clock = clock or time.monotonic
        self._spawn = spawn or _spawn_daemon
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._inflight: set[tuple[str, str]] = set()

    def get(self, slug: str | None, host: str = GITHUB) -> dict:
        """Cached stats for ``(host, slug)``; schedules a background refresh when stale."""
        if not slug:
            return unavailable(None, "no-remote", host=host)
        key = (host, slug)
        with self._lock:
            entry = self._entries.get(key)
            now = self._clock()
            fresh = entry is not None and (now - entry.fetched_at) < self._ttl
            if fresh:
                return entry.stats
            schedule = key not in self._inflight
            if schedule:
                self._inflight.add(key)
        if schedule:
            self._spawn(lambda: self._refresh(slug, host))
        with self._lock:
            entry = self._entries.get(key)
        return entry.stats if entry is not None else unavailable(slug, "pending", host=host)

    def _refresh(self, slug: str, host: str = GITHUB) -> None:
        try:
            stats = self._fetcher(slug, host)
        except Exception:  # pragma: no cover - fetcher already degrades to a dict
            stats = unavailable(slug, "gh-unavailable", host=host)
        with self._lock:
            self._entries[(host, slug)] = _Entry(stats, self._clock())
            self._inflight.discard((host, slug))
