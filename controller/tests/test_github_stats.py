# ABOUTME: Tests for github_stats — GitHub + GitLab repo-health fetch + per-slug TTL cache.
# ABOUTME: Story 11.2-006 / 23.7-001; all gh/glab calls are stubbed, so there is no live CLI dependency.

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


def test_unavailable_carries_host_for_wording() -> None:
    # Story 23.7-001: the sentinel names its forge so the client renders
    # host-appropriate wording ("GitLab unavailable" vs "GitHub unavailable").
    assert gh.unavailable("o/r", "glab-unavailable", host=gh.GITLAB)["host"] == gh.GITLAB
    assert gh.unavailable(None, "no-remote")["host"] is None


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
    assert s["host"] == gh.GITHUB
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


def test_run_gh_returns_stdout_on_success(monkeypatch) -> None:
    class R:
        returncode = 0
        stdout = "42\n"
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: R())
    assert gh._run_gh(["api", "x"]) == "42\n"


def test_count_parses_int_and_strips() -> None:
    assert gh._count("  7\n") == 7


def test_count_none_on_none_input() -> None:
    assert gh._count(None) is None


def test_count_none_on_non_numeric() -> None:
    assert gh._count("not-a-number") is None


# --- GitLab fetch (Story 23.7-001) ------------------------------------------


def _stub_glab(monkeypatch, mapping):
    """Stub ``_run_glab`` so a (args→stdout) mapping drives the GitLab fetch.

    Matches the first needle that is a substring of the joined args (insertion
    order), so list the more-specific endpoints (``pipelines``) first; value
    None simulates a failing call.
    """
    def fake(args, timeout=gh._GH_TIMEOUT):
        joined = " ".join(args)
        for needle, out in mapping.items():
            if needle in joined:
                return out
        return None
    monkeypatch.setattr(gh, "_run_glab", fake)


def test_normalize_gitlab_ci_maps_statuses() -> None:
    p = {"status": "success", "ref": "main", "created_at": "2026-06-20T10:00:00Z"}
    assert gh.normalize_gitlab_ci(p) == {
        "status": "success", "branch": "main", "created_at": "2026-06-20T10:00:00Z"}
    assert gh.normalize_gitlab_ci({"status": "failed"})["status"] == "failure"
    assert gh.normalize_gitlab_ci({"status": "canceled"})["status"] == "cancelled"
    assert gh.normalize_gitlab_ci({"status": "running"})["status"] == "in_progress"
    assert gh.normalize_gitlab_ci({"status": "weird"})["status"] == "unknown"


def test_normalize_gitlab_ci_none() -> None:
    assert gh.normalize_gitlab_ci(None) is None
    assert gh.normalize_gitlab_ci({}) is None  # no status → no CI signal


def test_fetch_gitlab_stats_happy_path(monkeypatch) -> None:
    pipeline = json.dumps([{"status": "success", "ref": "main",
                            "created_at": "2026-06-20T10:00:00Z"}])
    _stub_glab(monkeypatch, {
        "pipelines?ref=main": pipeline,               # most specific first
        "projects/owner%2Frepo": json.dumps({"default_branch": "main"}),
        "issue list -R owner/repo --closed": json.dumps([{}, {}]),   # 2 closed
        "issue list -R owner/repo --output": json.dumps([{}, {}, {}]),  # 3 open
        "mr list -R owner/repo --closed": json.dumps([{}]),          # 1 closed
        "mr list -R owner/repo --output": json.dumps([]),            # 0 open
    })
    s = gh.fetch_stats("owner/repo", host=gh.GITLAB)
    assert s["available"] is True
    assert s["slug"] == "owner/repo" and s["host"] == gh.GITLAB
    assert s["issues_open"] == 3 and s["issues_closed"] == 2
    assert s["prs_open"] == 0 and s["prs_closed"] == 1
    assert s["ci_status"] == "success" and s["ci_branch"] == "main"
    assert s["ci_created_at"] == "2026-06-20T10:00:00Z"


def test_fetch_gitlab_stats_all_calls_fail_is_unavailable(monkeypatch) -> None:
    # glab absent / unauthenticated → every call returns None → unavailable.
    _stub_glab(monkeypatch, {})
    s = gh.fetch_stats("owner/repo", host=gh.GITLAB)
    assert s["available"] is False
    assert s["reason"] == "glab-unavailable" and s["host"] == gh.GITLAB


def test_fetch_gitlab_stats_counts_ok_but_no_pipeline(monkeypatch) -> None:
    # Counts resolve, default branch resolves, but there is no pipeline yet.
    _stub_glab(monkeypatch, {
        "pipelines?ref=main": "[]",
        "projects/owner%2Frepo": json.dumps({"default_branch": "main"}),
        "issue list -R owner/repo --closed": "[]",
        "issue list -R owner/repo --output": json.dumps([{}]),
        "mr list -R owner/repo --closed": "[]",
        "mr list -R owner/repo --output": "[]",
    })
    s = gh.fetch_stats("owner/repo", host=gh.GITLAB)
    assert s["available"] is True
    assert s["issues_open"] == 1
    assert s["ci_status"] is None and s["ci_branch"] is None


def test_fetch_stats_gitlab_no_slug_is_no_remote() -> None:
    s = gh.fetch_stats(None, host=gh.GITLAB)
    assert s["available"] is False and s["reason"] == "no-remote"
    assert s["host"] == gh.GITLAB


def test_run_glab_returns_none_on_oserror(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("glab not installed")
    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert gh._run_glab(["issue", "list"]) is None


def test_run_glab_returns_stdout_on_success(monkeypatch) -> None:
    class R:
        returncode = 0
        stdout = "[]\n"
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: R())
    assert gh._run_glab(["mr", "list"]) == "[]\n"


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

    def fetcher(slug, host):
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

    def fetcher(slug, host):
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

    def fetcher(slug, host):
        calls.append(slug)
        return {"available": True, "slug": slug}

    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher, clock=_Clock(), spawn=_sync_spawn)
    cache.get("a/one")
    cache.get("a/one")
    cache.get("b/two")
    # One fetch per distinct slug, regardless of repeated gets.
    assert sorted(calls) == ["a/one", "b/two"]


def test_cache_keys_by_host_and_slug() -> None:
    # Story 23.7-001: the same slug on different forges is different data, so the
    # cache keys on (host, slug) and passes the host through to the fetcher.
    calls = []

    def fetcher(slug, host):
        calls.append((slug, host))
        return {"available": True, "slug": slug, "host": host}

    cache = gh.GitHubStatsCache(ttl=60.0, fetcher=fetcher, clock=_Clock(), spawn=_sync_spawn)
    assert cache.get("o/r", gh.GITHUB)["host"] == gh.GITHUB
    assert cache.get("o/r", gh.GITLAB)["host"] == gh.GITLAB
    cache.get("o/r", gh.GITHUB)  # cached — no third fetch
    assert calls == [("o/r", gh.GITHUB), ("o/r", gh.GITLAB)]


def test_cache_none_slug_no_fetch() -> None:
    calls = []
    cache = gh.GitHubStatsCache(
        ttl=60.0, fetcher=lambda s, h: calls.append(s), clock=_Clock(), spawn=_sync_spawn
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

    def fetcher(slug, host):
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
