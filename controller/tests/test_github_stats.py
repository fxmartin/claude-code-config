# ABOUTME: Tests for github_stats — GitHub repo-health fetch + per-slug TTL cache.
# ABOUTME: Story 11.2-006; all gh calls are stubbed, so there is no live `gh` dependency.

from __future__ import annotations

import json
import time

from sdlc import github_stats as gh


# --- pure parsing: CI normalization ----------------------------------------


def test_normalize_ci_completed_success() -> None:
    run = {"status": "completed", "conclusion": "success", "headBranch": "main",
           "createdAt": "2026-06-20T10:00:00Z"}
    ci = gh.normalize_ci(run)
    assert ci == {"status": "success", "branch": "main", "created_at": "2026-06-20T10:00:00Z"}


def test_normalize_ci_completed_failure() -> None:
    assert gh.normalize_ci({"status": "completed", "conclusion": "failure"})["status"] == "failure"


def test_normalize_ci_completed_cancelled() -> None:
    assert gh.normalize_ci({"status": "completed", "conclusion": "cancelled"})["status"] == "cancelled"


def test_normalize_ci_in_progress_maps_running_status() -> None:
    # A queued/in_progress run has no conclusion yet → reported as in_progress.
    assert gh.normalize_ci({"status": "in_progress", "conclusion": None})["status"] == "in_progress"
    assert gh.normalize_ci({"status": "queued", "conclusion": None})["status"] == "in_progress"


def test_normalize_ci_none() -> None:
    assert gh.normalize_ci(None) is None


def test_parse_ci_runs_picks_first() -> None:
    payload = json.dumps([
        {"status": "completed", "conclusion": "success", "headBranch": "main", "createdAt": "t1"},
        {"status": "completed", "conclusion": "failure", "headBranch": "main", "createdAt": "t0"},
    ])
    assert gh.parse_ci_runs(payload)["status"] == "success"


def test_parse_ci_runs_empty_or_bad() -> None:
    assert gh.parse_ci_runs("[]") is None
    assert gh.parse_ci_runs("not json") is None
    assert gh.parse_ci_runs(None) is None


# --- unavailable sentinel ----------------------------------------------------


def test_unavailable_shape() -> None:
    s = gh.unavailable("owner/repo", "gh-unavailable")
    assert s["available"] is False
    assert s["slug"] == "owner/repo"
    assert s["reason"] == "gh-unavailable"
    # Every count/CI field present and null so the client never throws.
    for k in ("issues_open", "issues_closed", "prs_open", "prs_closed",
              "ci_status", "ci_branch", "ci_created_at"):
        assert s[k] is None


# --- fetch_stats with a stubbed gh runner -----------------------------------


def _stub_gh(monkeypatch, mapping):
    """Stub ``_run_gh`` so a (args→stdout) mapping drives fetch without live gh.

    ``mapping`` keys are matched as a substring of the joined args; value None
    simulates a failing call.
    """
    def fake(args, timeout=gh._GH_TIMEOUT):
        joined = " ".join(args)
        for needle, out in mapping.items():
            if needle in joined:
                return out
        return None
    monkeypatch.setattr(gh, "_run_gh", fake)


def test_fetch_stats_happy_path(monkeypatch) -> None:
    runs = json.dumps([{"status": "completed", "conclusion": "success",
                        "headBranch": "main", "createdAt": "2026-06-20T10:00:00Z"}])
    _stub_gh(monkeypatch, {
        "type:issue state:open": "7",
        "type:issue state:closed": "120",
        "type:pr state:open": "3",
        "type:pr state:closed": "88",
        "repos/owner/repo": "main",   # default_branch
        "run list": runs,
    })
    s = gh.fetch_stats("owner/repo")
    assert s["available"] is True
    assert s["slug"] == "owner/repo"
    assert s["issues_open"] == 7 and s["issues_closed"] == 120
    assert s["prs_open"] == 3 and s["prs_closed"] == 88
    assert s["ci_status"] == "success" and s["ci_branch"] == "main"
    assert s["ci_created_at"] == "2026-06-20T10:00:00Z"


def test_fetch_stats_no_slug_is_no_remote() -> None:
    s = gh.fetch_stats(None)
    assert s["available"] is False and s["reason"] == "no-remote"


def test_fetch_stats_all_calls_fail_is_unavailable(monkeypatch) -> None:
    # gh absent / unauthenticated / rate-limited → every call returns None.
    _stub_gh(monkeypatch, {})
    s = gh.fetch_stats("owner/repo")
    assert s["available"] is False and s["reason"] == "gh-unavailable"


def test_fetch_stats_counts_ok_but_no_ci_runs(monkeypatch) -> None:
    # Counts resolve, default branch resolves, but the branch has no CI runs yet.
    _stub_gh(monkeypatch, {
        "type:issue state:open": "1",
        "type:issue state:closed": "2",
        "type:pr state:open": "0",
        "type:pr state:closed": "5",
        "repos/owner/repo": "main",
        "run list": "[]",
    })
    s = gh.fetch_stats("owner/repo")
    assert s["available"] is True
    assert s["ci_status"] is None and s["ci_branch"] is None


def test_run_gh_returns_none_on_oserror(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("gh not installed")
    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert gh._run_gh(["api", "x"]) is None


def test_run_gh_returns_none_on_nonzero(monkeypatch) -> None:
    class R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: R())
    assert gh._run_gh(["api", "x"]) is None


# --- per-slug TTL cache, off the request path -------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _sync_spawn(fn):
    """Run the refresh inline so cache behaviour is deterministic in tests."""
    fn()


def test_cache_fetches_once_and_serves_cached() -> None:
    calls = []

    def fetcher(slug):
        calls.append(slug)
        return {"available": True, "slug": slug, "issues_open": len(calls)}

    clock = _Clock()
    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher, clock=clock, spawn=_sync_spawn)

    first = cache.get("owner/repo")
    assert first["issues_open"] == 1
    # Within TTL → served from cache, no second fetch.
    again = cache.get("owner/repo")
    assert again["issues_open"] == 1
    assert calls == ["owner/repo"]


def test_cache_refetches_after_ttl() -> None:
    calls = []

    def fetcher(slug):
        calls.append(slug)
        return {"available": True, "slug": slug, "issues_open": len(calls)}

    clock = _Clock()
    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher, clock=clock, spawn=_sync_spawn)
    assert cache.get("owner/repo")["issues_open"] == 1
    clock.t += 61.0  # past the TTL
    assert cache.get("owner/repo")["issues_open"] == 2
    assert calls == ["owner/repo", "owner/repo"]


def test_cache_dedups_per_slug() -> None:
    calls = []

    def fetcher(slug):
        calls.append(slug)
        return {"available": True, "slug": slug}

    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher, clock=_Clock(), spawn=_sync_spawn)
    cache.get("a/one")
    cache.get("a/one")
    cache.get("b/two")
    # One fetch per distinct slug, regardless of repeated gets.
    assert sorted(calls) == ["a/one", "b/two"]


def test_cache_none_slug_no_fetch() -> None:
    calls = []
    cache = gh.GitHubStatsCache(
        ttl=60.0, fetcher=lambda s: calls.append(s), clock=_Clock(), spawn=_sync_spawn
    )
    s = cache.get(None)
    assert s["available"] is False and s["reason"] == "no-remote"
    assert calls == []


def test_cache_default_uses_background_thread() -> None:
    # With the default spawn, the fetch runs on a daemon thread: get() returns a
    # dict immediately (never blocks, never raises) and the cache is populated
    # for a subsequent poll. A barrier proves the request path does not wait on
    # the (here, blocked) fetcher.
    import threading

    release = threading.Event()
    started = threading.Event()

    def fetcher(slug):
        started.set()
        release.wait(timeout=5.0)  # hold the fetch open while we prove get() returned
        return {"available": True, "slug": slug, "issues_open": 9}

    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher)
    first = cache.get("owner/repo")
    assert isinstance(first, dict)          # returned without blocking on the fetch
    assert first["available"] is False      # pending sentinel while the fetch runs
    assert started.wait(timeout=5.0)        # the background fetch did start
    release.set()                           # let it finish
    # Background fetch populated the cache for the next poll.
    s = first
    for _ in range(100):
        s = cache.get("owner/repo")
        if s.get("issues_open") == 9:
            break
        time.sleep(0.02)
    assert s["issues_open"] == 9
