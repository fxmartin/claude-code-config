"""Issue #726: a stale remote story branch must not block the deterministic CR open.

A retried story cuts ``feature/<id>`` fresh from base, so an earlier attempt's
push (no CR) makes the controller's plain push non-fast-forward. These tests use
a real bare remote so the actual ``git push`` rejection is exercised.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from sdlc import issue_host as ih
from sdlc.build import BuildOptions, _open_story_cr

from test_build import _FakeCrAdapter, _mapped_ledger, _repo_with_undetectable_origin, _story


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def _stale_scenario(tmp_path: Path, story) -> tuple[Path, str, str]:
    """Remote holds a leftover attempt; local branch is a fresh cut from main.

    Returns (root, stale_sha, fresh_sha).
    """
    branch = f"feature/{story.id}"
    root = _repo_with_undetectable_origin(tmp_path, branch)
    stale = _commit(root, "attempt-1")
    _git(root, "push", "-u", "origin", branch)
    _git(root, "checkout", "-B", branch, "main")
    fresh = _commit(root, "attempt-2")
    return root, stale, fresh


class _Adapter(_FakeCrAdapter):
    def __init__(self, host: str, *, open_cr: str | None = None, find_raises: bool = False):
        super().__init__(host)
        self.open_cr = open_cr
        self.find_raises = find_raises
        self.find_calls: list[str] = []

    def cr_find(self, source_branch):
        self.find_calls.append(source_branch)
        if self.find_raises:
            raise ih.IssueHostError("lookup down")
        if self.open_cr is None:
            return None
        return ih.ChangeRequest(host=self.host, ref=self.open_cr, url="https://x/1")


def _open(tmp_path, monkeypatch, root, story, adapter):
    ledger = _mapped_ledger(tmp_path, story, "gitlab", "9")
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None, instance_url=None: adapter)
    pr = _open_story_cr(
        story, ledger, "run-1", root, "origin/main", None, ih.GITLAB_CR_TERMS,
        BuildOptions(), body="b", context="post-coverage",
    )
    with sqlite3.connect(ledger.db_path) as conn:
        events = [r[0] for r in conn.execute("SELECT message FROM events").fetchall()]
    return pr, events


def _remote_sha(root: Path, branch: str) -> str:
    return _git(root, "ls-remote", "origin", f"refs/heads/{branch}").split()[0]


def test_leftover_branch_without_open_cr_is_replaced_and_cr_opened(
    tmp_path, monkeypatch
) -> None:
    story = _story("09.4-002")
    root, stale, fresh = _stale_scenario(tmp_path, story)
    adapter = _Adapter("gitlab")

    pr, events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr == 42
    assert adapter.created
    assert _remote_sha(root, f"feature/{story.id}") == fresh
    assert any(stale in m and "stale" in m.lower() for m in events)
    # the old tip stays recoverable under a backup ref
    assert _git(root, "rev-parse", f"refs/sdlc/stale/{story.id}/{stale}") == stale


def test_open_cr_on_remote_branch_is_never_forced(tmp_path, monkeypatch) -> None:
    story = _story("09.4-002")
    root, stale, _fresh = _stale_scenario(tmp_path, story)
    adapter = _Adapter("gitlab", open_cr="7")

    pr, events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr is None
    assert adapter.created == []
    assert _remote_sha(root, f"feature/{story.id}") == stale
    assert any("non-fast-forward" in m for m in events)


def test_cr_lookup_failure_never_forces(tmp_path, monkeypatch) -> None:
    story = _story("09.4-002")
    root, stale, _fresh = _stale_scenario(tmp_path, story)
    adapter = _Adapter("gitlab", find_raises=True)

    pr, _events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr is None
    assert _remote_sha(root, f"feature/{story.id}") == stale


def test_lease_rejection_does_not_force(tmp_path, monkeypatch) -> None:
    """The remote moves between our ls-remote and the push: the lease must hold."""

    story = _story("09.4-002")
    root, stale, _fresh = _stale_scenario(tmp_path, story)
    branch = f"feature/{story.id}"
    adapter = _Adapter("gitlab")

    # Another writer advances the remote branch while we look up the CR.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", _git(root, "remote", "get-url", "origin"), str(other)],
        check=True, capture_output=True,
    )
    _git(other, "config", "user.email", "o@e.c")
    _git(other, "config", "user.name", "O")
    _git(other, "checkout", "-q", branch)
    moved = _commit(other, "concurrent")

    real_find = adapter.cr_find

    def find_then_advance(source_branch):
        result = real_find(source_branch)
        _git(other, "push", "origin", branch)
        return result

    adapter.cr_find = find_then_advance  # type: ignore[method-assign]
    pr, _events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr is None
    assert _remote_sha(root, branch) == moved


def test_non_rejection_push_failure_is_unchanged(tmp_path, monkeypatch) -> None:
    story = _story("09.4-002")
    root = _repo_with_undetectable_origin(tmp_path, f"feature/{story.id}")
    _git(root, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    adapter = _Adapter("gitlab")

    pr, events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr is None
    assert adapter.find_calls == []
    assert not any("stale" in m.lower() for m in events)


def test_clean_first_push_makes_no_ls_remote_call(tmp_path, monkeypatch) -> None:
    story = _story("09.4-002")
    root = _repo_with_undetectable_origin(tmp_path, f"feature/{story.id}")
    adapter = _Adapter("gitlab")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("sdlc.build.subprocess.run", spy)
    pr, _events = _open(tmp_path, monkeypatch, root, story, adapter)

    assert pr == 42
    assert not any("ls-remote" in c for c in calls)
    assert not any("--force-with-lease" in " ".join(c) for c in calls)
