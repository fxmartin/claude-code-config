# ABOUTME: GitHub repo-health fetch for the dashboard — issue/PR counts + default-branch CI.
# ABOUTME: Story 11.2-006 — per-repo-slug TTL cache, off the request path, graceful "unavailable".

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "GitHubStatsCache",
    "fetch_stats",
    "normalize_ci",
    "parse_ci_runs",
    "unavailable",
]

# `gh` talks to GitHub over the network; cap each call so a hung request can
# never wedge the background refresh. This budget is the *fetch* path's, never
# the dashboard request path's — the cache always serves the request.
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


# --- fetch ------------------------------------------------------------------


def unavailable(slug: str | None, reason: str) -> dict:
    """The muted sentinel rendered when GitHub data cannot be obtained.

    Every count/CI field is present and null so the client renders a uniform
    "GitHub unavailable" state and never throws on a missing key.
    """
    return {
        "available": False,
        "slug": slug,
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


def fetch_stats(slug: str | None) -> dict:
    """Fetch one repo's GitHub health (issues/PRs/CI), keyed by ``owner/repo``.

    Closed-PR count includes merged PRs (GitHub counts a merged PR as closed).
    When *every* underlying call fails the repo is reported unavailable
    (``gh`` missing / unauthenticated / rate-limited); a partial result (e.g.
    counts present but no CI runs yet) still renders as available.
    """
    if not slug:
        return unavailable(None, "no-remote")
    counts = {
        "issues_open": _search_count(slug, "type:issue state:open"),
        "issues_closed": _search_count(slug, "type:issue state:closed"),
        "prs_open": _search_count(slug, "type:pr state:open"),
        "prs_closed": _search_count(slug, "type:pr state:closed"),
    }
    ci = _fetch_ci(slug)
    if ci is None and all(v is None for v in counts.values()):
        return unavailable(slug, "gh-unavailable")
    return {
        "available": True,
        "slug": slug,
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
    """Serve GitHub stats from a per-slug cache, refreshing off the request path.

    A ``get(slug)`` never blocks on ``gh``: a fresh entry is returned as-is; a
    stale/absent one returns the last-known value (or a pending sentinel) and
    schedules a background refresh. Refreshes are deduped per slug, so N runs in
    one repo — and any number of polling tabs — cost a single fetch per TTL
    window, keeping GitHub's rate limit safe. ``fetcher``/``clock``/``spawn`` are
    injectable so the cache is testable without threads or a live ``gh``.
    """

    def __init__(
        self,
        *,
        ttl: float = _DEFAULT_TTL,
        fetcher: Callable[[str], dict] | None = None,
        clock: Callable[[], float] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._ttl = ttl
        self._fetcher = fetcher or fetch_stats
        self._clock = clock or time.monotonic
        self._spawn = spawn or _spawn_daemon
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._inflight: set[str] = set()

    def get(self, slug: str | None) -> dict:
        """Cached stats for ``slug``; schedules a background refresh when stale."""
        if not slug:
            return unavailable(None, "no-remote")
        with self._lock:
            entry = self._entries.get(slug)
            now = self._clock()
            fresh = entry is not None and (now - entry.fetched_at) < self._ttl
            if fresh:
                return entry.stats
            schedule = slug not in self._inflight
            if schedule:
                self._inflight.add(slug)
        if schedule:
            self._spawn(lambda: self._refresh(slug))
        with self._lock:
            entry = self._entries.get(slug)
        return entry.stats if entry is not None else unavailable(slug, "pending")

    def _refresh(self, slug: str) -> None:
        try:
            stats = self._fetcher(slug)
        except Exception:  # pragma: no cover - fetcher already degrades to a dict
            stats = unavailable(slug, "gh-unavailable")
        with self._lock:
            self._entries[slug] = _Entry(stats, self._clock())
            self._inflight.discard(slug)
