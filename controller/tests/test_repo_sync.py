# ABOUTME: Tests for a fleet worker's repo auto-sync before dispatch (Story 35.2-002).
# ABOUTME: Real bare-origin fixtures: fast-forward, clone-if-absent, dirty refusal, origin mismatch.

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.queue_worker import (
    ForgeUnavailable,
    PreparedRepo,
    RepoRefused,
    WorkerProfile,
    prepare_repo,
    same_origin,
)
from sdlc.registry import Registry, RunRecord
from sdlc.scheduler import SchedulerConfig, run_queue

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


class Forge:
    """A bare ``origin`` plus the seed clone that advances its default branch."""

    def __init__(self, root: Path, branch: str = "main") -> None:
        self.branch = branch
        self.bare = root / "forge" / "proj.git"
        self.bare.mkdir(parents=True)
        git(self.bare, "init", "-q", "--bare", "-b", branch)
        self.seed = root / "seed"
        git(root, "clone", "-q", str(self.bare), str(self.seed))
        git(self.seed, "checkout", "-q", "-b", branch)
        commit(self.seed, "README.md")
        git(self.seed, "push", "-q", "origin", branch)

    @property
    def url(self) -> str:
        return str(self.bare)

    def advance(self, name: str = "next.txt") -> str:
        sha = commit(self.seed, name)
        git(self.seed, "push", "-q", "origin", self.branch)
        return sha

    def go_down(self) -> None:
        """Stop answering, as a rebooting forge does: git calls to it fail at once."""
        self.bare.rename(self.bare.with_name("offline.git"))

    def come_back(self) -> None:
        self.bare.with_name("offline.git").rename(self.bare)


@pytest.fixture
def forge(tmp_path: Path) -> Forge:
    return Forge(tmp_path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    path = tmp_path / "Work"
    path.mkdir()
    return path


def _clone(forge: Forge, where: Path) -> Path:
    git(where.parent, "clone", "-q", forge.url, str(where))
    return where


def _job(tmp_path: Path, repo: Path, origin: str | None, **fields):
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    requirements = json.dumps({"origin": origin}) if origin else None
    job_id = store.add_job(
        repo=str(repo), kind="build", scope="epic-1", requirements_json=requirements, **fields
    )
    job = store.get_job(job_id)
    assert job is not None
    return job


# --- prepare_repo: the building blocks, against real git ---------------------


def test_a_stale_clone_is_fast_forwarded_to_the_forge_main(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    new_head = forge.advance()
    job = _job(tmp_path, clone, forge.url)

    prepared = prepare_repo(job, work_dir=work_dir)

    assert prepared == PreparedRepo(path=clone, sha=new_head)
    assert git(clone, "rev-parse", "HEAD") == new_head
    assert (clone / "next.txt").exists()


def test_a_clone_on_a_feature_branch_is_brought_back_to_main(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "feature/old")
    new_head = forge.advance()
    job = _job(tmp_path, clone, forge.url)

    prepared = prepare_repo(job, work_dir=work_dir)

    assert prepared.sha == new_head
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_an_absent_repo_is_cloned_from_the_recorded_origin(tmp_path, forge, work_dir) -> None:
    target = work_dir / "proj"
    job = _job(tmp_path, target, forge.url)

    prepared = prepare_repo(job, work_dir=work_dir)

    assert prepared.path == target
    assert prepared.sha == git(forge.seed, "rev-parse", "HEAD")
    assert git(target, "remote", "get-url", "origin") == forge.url
    assert git(target, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_repo_path_from_another_machine_is_cloned_under_the_work_dir(
    tmp_path, forge, work_dir
) -> None:
    """The job's recorded path may not exist here; the clone lands in ``work_dir``."""
    job = _job(tmp_path, Path("/Users/someone-else/Work/proj"), forge.url)

    prepared = prepare_repo(job, work_dir=work_dir)

    assert prepared.path == work_dir / "proj"
    assert (prepared.path / ".git").exists()


@pytest.mark.parametrize(
    "recorded_home",
    ["/home/fxmartin", "/Users/fxmartin"],  # the XPS (Linux) and the Macs
)
def test_the_enqueuers_home_maps_to_this_workers_home_whatever_the_user_name(
    tmp_path, forge, monkeypatch, recorded_home
) -> None:
    """Story 35.2-008: `dev` (user ``fx``) is the first worker whose login differs from the XPS's.

    The job carries ``/home/fxmartin/Work/proj``; no such path exists on this
    worker, and the clone lands under *its* home — ``~/Work/proj`` — by the same
    rule that maps it to ``/Users/fxmartin/…`` on a Mac. Nothing under the
    recorded home is read or created.
    """
    home = tmp_path / "home" / "fx"
    monkeypatch.setenv("HOME", str(home))
    recorded = Path(recorded_home) / "Work" / "proj"
    assert not recorded.exists()
    job = _job(tmp_path, recorded, forge.url)

    prepared = prepare_repo(job)  # the default work dir: ~/Work of the worker's own login

    assert prepared.path == (home / "Work" / "proj").resolve()
    assert (prepared.path / ".git").exists()
    assert prepared.sha == git(forge.seed, "rev-parse", "HEAD")
    assert not recorded.exists()


def test_a_tracked_dirty_file_refuses_the_job_and_is_never_stashed(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    (clone / "README.md").write_text("my uncommitted work\n", encoding="utf-8")
    forge.advance()
    head_before = git(clone, "rev-parse", "HEAD")
    job = _job(tmp_path, clone, forge.url)

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is True  # back to `queued`, not parked
    assert not isinstance(refusal.value, ForgeUnavailable)  # the clone's, not the forge's
    assert "DIRTY_WORKING_TREE" in refusal.value.reason
    assert "README.md" in refusal.value.reason
    assert git(clone, "stash", "list") == ""
    assert git(clone, "rev-parse", "HEAD") == head_before
    assert (clone / "README.md").read_text(encoding="utf-8") == "my uncommitted work\n"


def test_untracked_scratch_files_do_not_refuse_the_job(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    (clone / "scratch.txt").write_text("notes\n", encoding="utf-8")
    new_head = forge.advance()

    prepared = prepare_repo(_job(tmp_path, clone, forge.url), work_dir=work_dir)

    assert prepared.sha == new_head


def test_a_different_origin_refuses_the_job_naming_both_urls(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    elsewhere = "http://gitlab.test/root/other.git"
    job = _job(tmp_path, clone, elsewhere)

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is False
    assert "origin mismatch" in refusal.value.reason
    assert forge.url in refusal.value.reason
    assert elsewhere in refusal.value.reason


def test_a_diverged_local_main_is_refused_and_left_untouched(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    local_only = commit(clone, "local.txt")
    forge.advance()
    job = _job(tmp_path, clone, forge.url)

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is False
    assert "fast-forward" in refusal.value.reason
    assert "diverged" in refusal.value.reason
    assert git(clone, "rev-parse", "HEAD") == local_only


def test_a_merge_stopped_by_local_changes_is_not_called_divergence(
    tmp_path, forge, work_dir
) -> None:
    """The progress render is exempt from the dirty check yet can still abort the merge."""
    view = "docs/stories/.build-progress.md"
    (forge.seed / "docs" / "stories").mkdir(parents=True)
    forge.advance(view)
    clone = _clone(forge, work_dir / "proj")
    commit(forge.seed, view, "newer render\n")
    git(forge.seed, "push", "-q", "origin", "main")
    (clone / view).write_text("local render\n", encoding="utf-8")
    job = _job(tmp_path, clone, forge.url)

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is False
    assert "fast-forward" in refusal.value.reason
    assert "diverged" not in refusal.value.reason


def test_an_ancestry_check_that_cannot_run_names_no_divergence(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, forge.url)
    real = queue_worker._git

    def merge_fails(root, *args):
        if args[0] == "merge":
            return subprocess.CompletedProcess(args, 1, "", "merge refused")
        if args[0] == "merge-base":
            raise FileNotFoundError("git")
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", merge_fails)
    with pytest.raises(RepoRefused, match="merge refused") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "diverged" not in refusal.value.reason


def test_a_failed_clone_refuses_the_job(tmp_path, work_dir) -> None:
    """A forge that cannot be reached fails the clone at once; like a fetch, it is retried."""
    job = _job(tmp_path, work_dir / "proj", str(tmp_path / "no-such-forge.git"))

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "clone" in refusal.value.reason
    assert isinstance(refusal.value, ForgeUnavailable)
    assert refusal.value.retryable is True
    assert not (work_dir / "proj").exists()


def test_a_forge_that_cannot_be_reached_sends_the_job_back_with_git_s_error(
    tmp_path, forge, work_dir
) -> None:
    """A forge that is down fails `git fetch` at once, not by timing out — and comes back."""
    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, forge.url)
    forge.go_down()

    with pytest.raises(ForgeUnavailable, match="could not fetch origin") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is True
    assert "Could not read from remote repository" in refusal.value.reason


def test_a_clone_another_live_job_is_using_is_refused_before_git_touches_it(
    tmp_path, forge, work_dir
) -> None:
    """The sync checks out and fast-forwards the clone itself — a write to its checkout."""
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "feature/1.1-001")
    forge.advance()

    with pytest.raises(RepoRefused, match="repo busy") as refusal:
        prepare_repo(
            _job(tmp_path, clone, forge.url), work_dir=work_dir, busy_repos={str(clone)}
        )

    assert refusal.value.retryable is True
    assert git(clone, "branch", "--show-current") == "feature/1.1-001"


def test_a_forge_whose_default_branch_is_not_main_syncs_that_branch(tmp_path, work_dir) -> None:
    """The build cuts story branches from ``origin/HEAD`` (Story 23.2-001); so does the sync."""
    forge = Forge(tmp_path, branch="trunk")
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "feature/old")
    new_head = forge.advance()

    prepared = prepare_repo(_job(tmp_path, clone, forge.url), work_dir=work_dir)

    assert prepared.sha == new_head
    assert git(clone, "branch", "--show-current") == "trunk"


def test_a_job_that_records_no_origin_is_left_alone(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    head = git(clone, "rev-parse", "HEAD")
    forge.advance()

    prepared = prepare_repo(_job(tmp_path, clone, None), work_dir=work_dir)

    assert prepared == PreparedRepo(path=clone, sha=None)
    assert git(clone, "rev-parse", "HEAD") == head


def test_a_directory_that_is_not_a_clone_refuses_the_job(tmp_path, forge, work_dir) -> None:
    not_a_clone = work_dir / "proj"
    not_a_clone.mkdir()

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(_job(tmp_path, not_a_clone, forge.url), work_dir=work_dir)

    assert refusal.value.retryable is False
    assert "not a git clone" in refusal.value.reason


# --- origin helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("http://gitlab.test/root/proj.git", "http://gitlab.test/root/proj", True),
        ("http://gitlab.test/root/proj.git/", "http://GitLab.test/root/proj.git", True),
        ("git@github.com:fxmartin/proj.git", "https://github.com/fxmartin/proj", True),
        ("ssh://git@gitlab.test/root/proj.git", "http://gitlab.test/root/proj.git", True),
        ("http://gitlab.test/root/proj.git", "http://gitlab.test/root/other.git", False),
        ("http://gitlab.test/root/proj.git", "https://github.com/root/proj.git", False),
        ("/srv/forge/proj.git", "/srv/forge/proj.git", True),
        ("/srv/forge/proj.git", "/srv/other/proj.git", False),
    ],
)
def test_origin_urls_compare_by_host_and_path(left: str, right: str, same: bool) -> None:
    assert same_origin(left, right) is same


def test_a_clone_with_no_origin_remote_is_an_origin_mismatch(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    git(clone, "remote", "remove", "origin")

    with pytest.raises(RepoRefused, match=r"has origin \(none\)") as refusal:
        prepare_repo(_job(tmp_path, clone, forge.url), work_dir=work_dir)

    assert refusal.value.retryable is False


def test_malformed_or_non_object_requirements_count_as_no_recorded_origin(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, None)
    for raw in ("{not json", json.dumps(["origin"]), json.dumps({"origin": "  "})):
        prepared = prepare_repo(replace(job, requirements=raw), work_dir=work_dir)
        assert prepared == PreparedRepo(path=clone, sha=None)


def test_many_dirty_files_are_summarised_in_the_refusal(tmp_path, forge, work_dir) -> None:
    from sdlc.build import _DIRTY_TREE_MAX_LISTED

    clone = _clone(forge, work_dir / "proj")
    extra = _DIRTY_TREE_MAX_LISTED + 3
    for i in range(extra):
        commit(clone, f"f{i}.txt")
    for i in range(extra):
        (clone / f"f{i}.txt").write_text("changed\n", encoding="utf-8")
    job = _job(tmp_path, clone, forge.url)

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "(+3 more)" in refusal.value.reason


def test_git_that_cannot_run_is_a_refusal_not_a_crash(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, forge.url)
    real = queue_worker._git

    def failing(root, *args):
        if args[0] == "fetch":
            raise FileNotFoundError("git")
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", failing)
    with pytest.raises(RepoRefused, match="git fetch failed") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is False


def test_a_git_timeout_sends_the_job_back_to_be_retried(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """A slow forge is transient: retry the job rather than park it for an operator."""
    from sdlc import queue_worker

    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, forge.url)
    real = queue_worker._git

    def slow(root, *args):
        if args[0] == "fetch":
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=1)
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", slow)
    with pytest.raises(ForgeUnavailable, match="git fetch timed out") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is True


def test_a_clone_that_times_out_leaves_nothing_behind_and_is_retried(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """A killed `git clone` cannot clean up after itself; a half clone would read as dirty."""
    from sdlc import queue_worker

    target = work_dir / "proj"
    job = _job(tmp_path, target, forge.url)

    def killed(argv, **_kwargs):
        (target / ".git").mkdir(parents=True)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(queue_worker.subprocess, "run", killed)
    with pytest.raises(ForgeUnavailable, match="timed out") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is True
    assert not target.exists()


def test_an_origin_that_reads_as_a_git_option_is_never_handed_to_git(
    tmp_path, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    calls: list[list[str]] = []
    monkeypatch.setattr(
        queue_worker.subprocess, "run", lambda argv, **_k: calls.append(list(argv))
    )
    job = _job(tmp_path, work_dir / "proj", "--upload-pack=touch pwned")

    with pytest.raises(RepoRefused, match="git option") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert refusal.value.retryable is False
    assert calls == []


def test_the_clone_ends_option_parsing_before_the_origin(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    seen: list[list[str]] = []
    real = queue_worker.subprocess.run

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(queue_worker.subprocess, "run", spy)
    prepare_repo(_job(tmp_path, work_dir / "proj", forge.url), work_dir=work_dir)

    clone_argv = next(argv for argv in seen if argv[:2] == ["git", "clone"])
    assert clone_argv[-3:] == ["--", forge.url, str(work_dir / "proj")]


def test_an_unreadable_head_after_syncing_is_a_refusal(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    clone = _clone(forge, work_dir / "proj")
    job = _job(tmp_path, clone, forge.url)
    real = queue_worker._git

    def no_head(root, *args):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", no_head)
    with pytest.raises(RepoRefused, match="could not read HEAD"):
        prepare_repo(job, work_dir=work_dir)


def test_a_clone_that_cannot_launch_git_is_a_refusal(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    job = _job(tmp_path, work_dir / "absent", forge.url)

    def boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(queue_worker.subprocess, "run", boom)
    with pytest.raises(RepoRefused, match="could not clone"):
        prepare_repo(job, work_dir=work_dir)


def test_a_work_dir_that_cannot_hold_the_clone_is_a_refusal_not_a_crash(tmp_path, forge) -> None:
    """The drain catches refusals only: an `OSError` here would take the whole worker down."""
    not_a_dir = tmp_path / "Work"
    not_a_dir.write_text("", encoding="utf-8")  # a file where the clone's parent should be
    job = _job(tmp_path, tmp_path / "elsewhere" / "proj", forge.url)

    with pytest.raises(RepoRefused, match="could not clone") as refusal:
        prepare_repo(job, work_dir=not_a_dir)

    assert refusal.value.retryable is False  # an operator's to fix: parked `blocked`


def test_origin_requirements_records_the_origin_json_or_none(tmp_path, forge, work_dir) -> None:
    from sdlc.queue_worker import origin_requirements

    clone = _clone(forge, work_dir / "proj")
    assert json.loads(origin_requirements(clone)) == {"origin": forge.url}
    assert origin_requirements(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("configured", "recorded"),
    [
        ("https://oauth2:s3cret@gitlab.test/root/proj.git", "https://gitlab.test/root/proj.git"),
        # A GitHub token rides in the user slot, so http(s) drops the whole userinfo.
        ("https://ghp_s3cret@github.com/fx/proj.git", "https://github.com/fx/proj.git"),
        ("http://gitlab.test:8080/root/proj.git", "http://gitlab.test:8080/root/proj.git"),
        # An ssh user is the forge's account (`git`), not a secret — dropping it
        # would break the clone; only a password goes.
        (
            "ssh://git:s3cret@gitlab.test:2222/root/proj.git",
            "ssh://git@gitlab.test:2222/root/proj.git",
        ),
        ("ssh://git@gitlab.test/root/proj.git", "ssh://git@gitlab.test/root/proj.git"),
        ("git@github.com:fx/proj.git", "git@github.com:fx/proj.git"),
        ("/srv/forge/proj.git", "/srv/forge/proj.git"),
    ],
)
def test_the_recorded_origin_never_carries_a_credential(
    tmp_path, configured: str, recorded: str
) -> None:
    """The job is stored, served by `GET /jobs` and printed by `queue list --json`."""
    from sdlc.queue_worker import origin_requirements

    git(tmp_path, "init", "-q", "proj")
    git(tmp_path / "proj", "remote", "add", "origin", configured)

    assert json.loads(origin_requirements(tmp_path / "proj")) == {"origin": recorded}


def test_an_origin_mismatch_names_both_origins_without_their_credentials(
    tmp_path, forge, work_dir
) -> None:
    """The reason is a field every queue client reads; a worker's token must not land there."""
    clone = _clone(forge, work_dir / "proj")
    git(clone, "remote", "set-url", "origin", "https://oauth2:s3cret@gitlab.test/root/mine.git")
    job = _job(tmp_path, clone, "https://x-access-token:t0ken@gitlab.test/root/other.git")

    with pytest.raises(RepoRefused, match="origin mismatch") as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "https://gitlab.test/root/mine.git" in refusal.value.reason
    assert "https://gitlab.test/root/other.git" in refusal.value.reason
    assert "s3cret" not in refusal.value.reason
    assert "t0ken" not in refusal.value.reason


def test_a_failed_clone_names_the_origin_without_its_credential(
    tmp_path, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    monkeypatch.setattr(
        queue_worker.subprocess, "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 128, "", "fatal: not found"),
    )
    job = _job(tmp_path, work_dir / "proj", "https://oauth2:s3cret@gitlab.test/root/proj.git")

    with pytest.raises(
        RepoRefused, match="could not clone https://gitlab.test/root/proj.git"
    ) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "s3cret" not in refusal.value.reason


def test_a_checkout_held_up_by_another_git_s_lock_is_retried_not_parked(
    tmp_path, forge, work_dir
) -> None:
    """An IDE's brief ``index.lock`` clears on its own; `blocked` would need an operator."""
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "feature/old")
    forge.advance()
    (clone / ".git" / "index.lock").write_text("", encoding="utf-8")

    with pytest.raises(RepoRefused, match="index.lock") as refusal:
        prepare_repo(_job(tmp_path, clone, forge.url), work_dir=work_dir)

    assert refusal.value.retryable is True
    assert not isinstance(refusal.value, ForgeUnavailable)
    assert git(clone, "branch", "--show-current") == "feature/old"


# --- a slow fetch or clone keeps the worker alive -------------------------------


def _keepalive_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "sdlc-sync-keepalive"]


def test_a_slow_fetch_calls_the_keepalive_until_it_returns(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """The drain is blocked on the fetch, which can outlast the lease and the offline window."""
    from sdlc import queue_worker

    clone = _clone(forge, work_dir / "proj")
    monkeypatch.setattr(queue_worker, "_KEEPALIVE_SECONDS", 0.01)
    beat = threading.Event()
    real = queue_worker._git

    def slow_fetch(root, *args):
        if args[0] == "fetch":
            assert beat.wait(30), "no keepalive while the fetch was in flight"
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", slow_fetch)
    prepared = prepare_repo(
        _job(tmp_path, clone, forge.url), work_dir=work_dir, keepalive=beat.set
    )

    assert prepared.sha == git(forge.seed, "rev-parse", "HEAD")
    assert _keepalive_threads() == []


def test_a_slow_clone_calls_the_keepalive_until_it_returns(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    from sdlc import queue_worker

    monkeypatch.setattr(queue_worker, "_KEEPALIVE_SECONDS", 0.01)
    beat = threading.Event()
    real = queue_worker.subprocess.run

    def slow_clone(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            assert beat.wait(30), "no keepalive while the clone was in flight"
        return real(argv, **kwargs)

    monkeypatch.setattr(queue_worker.subprocess, "run", slow_clone)
    prepared = prepare_repo(
        _job(tmp_path, work_dir / "proj", forge.url), work_dir=work_dir, keepalive=beat.set
    )

    assert prepared.path == work_dir / "proj"
    assert _keepalive_threads() == []


def test_a_clone_interrupted_mid_way_leaves_nothing_behind(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """Ctrl-C kills git with SIGKILL, which cannot clean up; a half clone reads as dirty."""
    from sdlc import queue_worker

    target = work_dir / "proj"

    def interrupted(argv, **_kwargs):
        (target / ".git").mkdir(parents=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(queue_worker.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prepare_repo(_job(tmp_path, target, forge.url), work_dir=work_dir, keepalive=lambda: None)

    assert not target.exists()
    assert _keepalive_threads() == []


# --- the scheduler: the sync runs after the claim, before the launch ----------


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._polls = 1

    def poll(self) -> int | None:
        if self._polls > 0:
            self._polls -= 1
            return None
        return 0

    def stop(self) -> None:
        self._polls = 0


class FakeLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), str(cwd)))
        return FakeProc(90000 + len(self.calls))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


class RegisteringLauncher(FakeLauncher):
    """Registers each run the way `run_build` does: under the cwd it really runs in."""

    def __init__(self, registry: Registry, db: Path) -> None:
        super().__init__()
        self.registry = registry
        self.db = db

    def __call__(self, argv, cwd):
        proc = super().__call__(argv, cwd)
        self.registry.register(
            RunRecord(run_id=f"run-{proc.pid}", repo=str(Path(cwd).resolve()),
                      db=str(self.db), scope="epic-1", pid=proc.pid,
                      status="IN_PROGRESS", started_at="")
        )
        return proc


def _drain(
    tmp_path, store, work_dir, *, worker: bool = True, clock=None, launcher=None,
    registry=None, preparer=None, profile=None, follow: bool = False, sleeper=None,
    echo=None, **kwargs,
):
    clock = clock or Clock()
    launcher = launcher or FakeLauncher()
    if profile is None and worker:
        profile = WorkerProfile(name="xps", host="omarchy-xps13")
    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, follow=follow, worker=profile),
        registry=registry or Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=sleeper or clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=echo or (lambda _line: None),
        identity="xps",
        prepare_repo=preparer or partial(prepare_repo, work_dir=work_dir),
        **kwargs,
    )
    return result, launcher


def _enqueue(store: QueueStore, repo: Path, origin: str | None) -> int:
    return store.add_job(
        repo=str(repo), kind="build", scope="epic-1",
        requirements_json=json.dumps({"origin": origin}) if origin else None,
    )


def test_the_worker_syncs_then_launches_in_the_clone_and_records_the_sha(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    new_head = forge.advance()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, forge.url)

    _, launcher = _drain(tmp_path, store, work_dir)

    assert [cwd for _, cwd in launcher.calls] == [str(clone)]
    job = store.get_job(job_id)
    assert job is not None
    assert job.synced_sha == new_head
    assert job.to_dict()["synced_sha"] == new_head


def test_the_worker_clones_an_absent_repo_and_launches_there(tmp_path, forge, work_dir) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, work_dir / "proj", forge.url)

    _, launcher = _drain(tmp_path, store, work_dir)

    assert [cwd for _, cwd in launcher.calls] == [str(work_dir / "proj")]
    job = store.get_job(job_id)
    assert job is not None and job.synced_sha == git(forge.seed, "rev-parse", "HEAD")


def test_a_dirty_clone_sends_the_job_back_to_queued_with_the_reason(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    (clone / "README.md").write_text("dirty\n", encoding="utf-8")
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, forge.url)

    result, launcher = _drain(tmp_path, store, work_dir)

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by, job.worker) == ("queued", None, None)
    assert "DIRTY_WORKING_TREE" in (job.reason or "")
    assert launcher.calls == []
    assert result.started == 0
    assert git(clone, "stash", "list") == ""


def test_an_origin_mismatch_blocks_the_job_naming_the_mismatch(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, "http://gitlab.test/root/other.git")

    _, launcher = _drain(tmp_path, store, work_dir)

    job = store.get_job(job_id)
    assert job is not None
    assert job.state == "blocked"
    assert "origin mismatch" in (job.reason or "")
    assert launcher.calls == []


def _fetches(monkeypatch, clock: Clock) -> list[datetime]:
    """When, on ``clock``, each `git fetch` the sync runs was made."""
    from sdlc import queue_worker

    made: list[datetime] = []
    real = queue_worker._git

    def recording(root, *args):
        if args[0] == "fetch":
            made.append(clock())
        return real(root, *args)

    monkeypatch.setattr(queue_worker, "_git", recording)
    return made


def _gaps(moments: list[datetime]) -> list[float]:
    return [(later - earlier).total_seconds() for earlier, later in zip(moments, moments[1:])]


def test_a_forge_outage_leaves_the_queue_queued_and_it_drains_once_the_forge_is_back(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """The GitLab box rebooting must not park every queued job for a manual requeue.

    Nor cost a fetch per queued job: the forge failed the first one's, so the
    others on that origin wait on it too — and `queue list` says why.
    """
    clone = _clone(forge, work_dir / "proj")
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    ids = [_enqueue(store, clone, forge.url) for _ in range(3)]
    forge.go_down()
    clock = Clock()
    fetches = _fetches(monkeypatch, clock)

    result, launcher = _drain(tmp_path, store, work_dir, clock=clock)

    jobs = [store.get_job(job_id) for job_id in ids]
    assert [job.state for job in jobs if job] == ["queued"] * 3
    assert all("could not fetch origin" in (job.reason or "") for job in jobs if job)
    assert (launcher.calls, result.parked) == ([], 0)
    assert len(fetches) == 1

    forge.come_back()
    _, launcher = _drain(tmp_path, store, work_dir)

    # One at a time: each waits `repo busy` while another holds the clone.
    assert [cwd for _, cwd in launcher.calls] == [str(clone)] * 3


def test_a_plain_drain_never_touches_the_clone(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    head = git(clone, "rev-parse", "HEAD")
    forge.advance()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, forge.url)

    _, launcher = _drain(tmp_path, store, work_dir, worker=False)

    assert len(launcher.calls) == 1
    assert git(clone, "rev-parse", "HEAD") == head
    job = store.get_job(job_id)
    assert job is not None and job.synced_sha is None


def test_a_job_with_no_recorded_origin_launches_unsynced(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, None)

    _, launcher = _drain(tmp_path, store, work_dir)

    assert len(launcher.calls) == 1
    job = store.get_job(job_id)
    assert job is not None and job.synced_sha is None


# --- a path recorded on another machine: the clone is what every later step sees ---


def _elsewhere(tmp_path: Path) -> Path:
    """Where the enqueuing machine keeps the clone — a path that does not exist here."""
    return tmp_path / "Users" / "fx" / "Work" / "proj"


def test_a_job_from_another_machine_is_tracked_in_the_clone_it_ran_in(
    tmp_path, forge, work_dir
) -> None:
    """Run attach, resume, reconcile and the approval probe all read ``job.repo``."""
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, _elsewhere(tmp_path), forge.url)
    registry = Registry(tmp_path / "registry.json")
    launcher = RegisteringLauncher(registry, tmp_path / "ledger.db")

    _drain(tmp_path, store, work_dir, launcher=launcher, registry=registry)

    clone = work_dir / "proj"
    assert [cwd for _, cwd in launcher.calls] == [str(clone)]
    job = store.get_job(job_id)
    assert job is not None
    assert job.repo == str(clone)
    assert job.run_id == "run-90001"


def test_a_remapped_clone_already_running_a_fix_is_left_alone(
    tmp_path, forge, work_dir
) -> None:
    """The claim's per-repo exclusivity saw the foreign path; the clone must pass it too."""
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "fix/42")  # a fix runs in the repo root
    forge.advance()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    fix_id = store.add_job(repo=str(clone), kind="fix", scope="42")
    assert store.claim_job(
        fix_id, claimed_by="peer", lease_seconds=3600, now=Clock()()
    ) is not None
    build_id = _enqueue(store, _elsewhere(tmp_path), forge.url)

    result, launcher = _drain(tmp_path, store, work_dir)

    build = store.get_job(build_id)
    assert build is not None
    assert (build.state, build.claimed_by, build.repo) == (
        "queued", None, str(_elsewhere(tmp_path))
    )
    assert "repo busy" in (build.reason or "")
    assert launcher.calls == []
    assert result.started == 0
    assert git(clone, "branch", "--show-current") == "fix/42"


def test_a_build_live_in_the_clone_keeps_its_branch_when_a_second_build_is_claimed(
    tmp_path, forge, work_dir
) -> None:
    """A `--sequential` build works in the clone itself, on `feature/<id>`.

    The claim lets two builds share a repo (Story 32.1-003) because neither
    writes to the shared checkout mid-run. The sync does, so the second build
    waits for the clone instead of switching the first one's checkout to `main`.
    """
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "feature/1.1-001")  # clean, between two commits
    forge.advance()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    first = _enqueue(store, clone, forge.url)
    assert store.claim_job(
        first, claimed_by="peer", lease_seconds=3600, now=Clock()()
    ) is not None
    second = _enqueue(store, clone, forge.url)

    result, launcher = _drain(tmp_path, store, work_dir)

    job = store.get_job(second)
    assert job is not None
    assert (job.state, job.claimed_by) == ("queued", None)
    assert "repo busy" in (job.reason or "")
    assert launcher.calls == []
    assert result.started == 0
    assert git(clone, "branch", "--show-current") == "feature/1.1-001"


# --- a clone in use by any live run on this host, queue job or not ----------------


def _foreground_run(registry: Registry, clone: Path, *, run_id: str, pid: int) -> None:
    """A run started by hand in the clone — registered as `run_build`/`run_fix` do."""
    registry.register(
        RunRecord(run_id=run_id, repo=str(clone.resolve()), db=str(clone / ".sdlc-state.db"),
                  scope="42", pid=pid, status="IN_PROGRESS", started_at="")
    )


def test_a_foreground_run_live_in_the_clone_keeps_its_branch(tmp_path, forge, work_dir) -> None:
    """A hand-run `sdlc fix` works in the repo root and is no queue job; the registry sees it."""
    clone = _clone(forge, work_dir / "proj")
    git(clone, "checkout", "-q", "-b", "fix/42")  # clean, between two of its commits
    forge.advance()
    registry = Registry(tmp_path / "registry.json")
    _foreground_run(registry, clone, run_id="by-hand", pid=os.getpid())
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, forge.url)

    result, launcher = _drain(tmp_path, store, work_dir, registry=registry)

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by) == ("queued", None)
    assert "repo busy" in (job.reason or "")
    assert (launcher.calls, result.started) == ([], 0)
    assert git(clone, "branch", "--show-current") == "fix/42"


def test_a_finished_or_dead_run_does_not_hold_the_clone(tmp_path, forge, work_dir) -> None:
    clone = _clone(forge, work_dir / "proj")
    new_head = forge.advance()
    registry = Registry(tmp_path / "registry.json")
    _foreground_run(registry, clone, run_id="finished", pid=os.getpid())
    registry.mark_finished("finished", "DONE")
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    _foreground_run(registry, clone, run_id="crashed", pid=gone.pid)
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, clone, forge.url)

    _, launcher = _drain(tmp_path, store, work_dir, registry=registry)

    assert [cwd for _, cwd in launcher.calls] == [str(clone)]
    job = store.get_job(job_id)
    assert job is not None and job.synced_sha == new_head


class CountingStore(QueueStore):
    """Each job's claims, and when, to tell one claim from a claim on every poll."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.claims: list[int] = []
        self.claimed_at: list[datetime] = []

    def claim_job(self, job_id, **kwargs):
        self.claims.append(job_id)
        self.claimed_at.append(kwargs["now"])
        return super().claim_job(job_id, **kwargs)


def test_a_job_waiting_on_a_busy_clone_is_not_claimed_again_until_the_clone_frees(
    tmp_path, forge, work_dir
) -> None:
    """One claim learns the clone is busy; the polls after it leave the job queued."""
    clone = _clone(forge, work_dir / "proj")
    store = CountingStore(tmp_path / "queue.db")
    store.init()
    clock = Clock()
    holder = _enqueue(store, clone, forge.url)
    assert store.claim_job(holder, claimed_by="peer", lease_seconds=3600, now=clock()) is not None
    waiting = _enqueue(store, clone, forge.url)
    polls = {"n": 0}

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)
        polls["n"] += 1
        if polls["n"] == 5:
            assert store.finish_job(holder, "done", claimed_by="peer")  # the clone frees up
        if polls["n"] >= 8:
            raise KeyboardInterrupt

    _, launcher = _drain(tmp_path, store, work_dir, clock=clock, follow=True, sleeper=sleeper)

    assert store.claims.count(waiting) == 2  # the claim that found it busy, then the launch
    assert [cwd for _, cwd in launcher.calls] == [str(clone)]


# --- a refusal that clears on its own is retried on a doubling wait ------------------


def test_a_down_forge_is_fetched_once_per_doubling_wait_not_per_job_per_poll(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """A `--follow` worker must not fetch a down forge on every poll, once per queued job.

    A fetch to a forge that hangs holds the drain for up to the git timeout, so
    the jobs on that origin wait on one fetch together and the wait doubles to
    a cap; the first fetch that succeeds lets them all through.
    """
    clones = [_clone(forge, work_dir / name) for name in ("proj", "proj-2")]
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    for clone in clones:
        _enqueue(store, clone, forge.url)
    forge.go_down()
    clock = Clock()
    start = clock()
    fetches = _fetches(monkeypatch, clock)
    back = start + timedelta(seconds=2200)

    def sleeper(_seconds: float) -> None:
        clock.advance(5)
        if clock() == back:
            forge.come_back()
        if clock() >= start + timedelta(seconds=2800):
            raise KeyboardInterrupt

    _, launcher = _drain(tmp_path, store, work_dir, clock=clock, follow=True, sleeper=sleeper)

    during = [moment for moment in fetches if moment < back]
    assert _gaps(during) == [30, 60, 120, 240, 480, 600, 600]  # one fetch per wait, capped
    assert sorted(cwd for _, cwd in launcher.calls) == sorted(map(str, clones))


def test_a_dirty_clone_is_rechecked_once_per_doubling_wait_and_launches_once_tidied(
    tmp_path, forge, work_dir
) -> None:
    """A clone left dirty overnight costs a claim and a `git status` per wait, not per poll."""
    clone = _clone(forge, work_dir / "proj")
    (clone / "README.md").write_text("my uncommitted work\n", encoding="utf-8")
    store = CountingStore(tmp_path / "queue.db")
    store.init()
    _enqueue(store, clone, forge.url)
    clock = Clock()
    start = clock()

    def sleeper(_seconds: float) -> None:
        clock.advance(5)
        if clock() == start + timedelta(seconds=100):
            git(clone, "checkout", "--", "README.md")  # its owner tidies it
        if clock() >= start + timedelta(seconds=300):
            raise KeyboardInterrupt

    _, launcher = _drain(tmp_path, store, work_dir, clock=clock, follow=True, sleeper=sleeper)

    assert _gaps(store.claimed_at) == [30, 60, 120]  # dirty at 0, 30 and 90 s; clean at 210 s
    assert [cwd for _, cwd in launcher.calls] == [str(clone)]
    assert git(clone, "stash", "list") == ""


class LongRunningLauncher(FakeLauncher):
    """Each process it starts runs for ``passes`` scheduler passes."""

    def __init__(self, passes: int) -> None:
        super().__init__()
        self.passes = passes

    def __call__(self, argv, cwd):
        proc = super().__call__(argv, cwd)
        proc._polls = self.passes
        return proc


def test_a_plain_drain_retries_a_refused_job_on_its_wait_and_does_not_wait_it_out(
    tmp_path, work_dir
) -> None:
    """A running sibling keeps a plain drain polling, not re-syncing the refused job each poll.

    Once the sibling is done nothing holds the drain open for the refused job:
    it stays queued for the next `sdlc queue run`, as any refused job does.
    """
    down, up = Forge(tmp_path / "down"), Forge(tmp_path / "up")
    store = CountingStore(tmp_path / "queue.db")
    store.init()
    refused = _enqueue(store, _clone(down, work_dir / "down"), down.url)
    _enqueue(store, _clone(up, work_dir / "up"), up.url)
    down.go_down()

    result, _ = _drain(tmp_path, store, work_dir, launcher=LongRunningLauncher(passes=40))

    assert store.claims.count(refused) == 2  # at 0 s, then once its 30 s wait was up
    assert result.started == 1
    job = store.get_job(refused)
    assert job is not None and job.state == "queued"


# --- a fleet job, in 35.3-001's enqueue shape, on a worker without the clone ---------


def test_a_worker_without_the_clone_takes_a_fleet_job_and_clones_it(
    tmp_path, forge, work_dir, monkeypatch
) -> None:
    """`--enqueue` on a fleet records `repo` as a need; the recorded origin meets it (AC2).

    Enqueued from another machine through the live service, so the job names the
    repo and its origin but no path this worker has — and this worker has no clone.
    """
    from typer.testing import CliRunner

    from sdlc.cli import app
    from sdlc.queue_server import AccessPolicy, make_server

    service = QueueStore(tmp_path / "service.db")
    service.init()
    server = make_server(
        service, AccessPolicy(token="t0ken", networks=("127.0.0.0/8",)), "127.0.0.1", 0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / "laptop").mkdir()
    laptop = _clone(forge, tmp_path / "laptop" / "proj")
    try:
        with monkeypatch.context() as env:
            env.chdir(laptop)
            env.setenv("SDLC_QUEUE_URL", f"http://127.0.0.1:{server.server_address[1]}")
            env.setenv("SDLC_QUEUE_TOKEN", "t0ken")
            result = CliRunner().invoke(app, ["build", "epic-1", "--enqueue"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    assert result.exit_code == 0, result.output
    shutil.rmtree(laptop)  # the enqueuing machine's path means nothing on this worker
    (job,) = service.list_jobs()
    assert json.loads(job.requirements or "{}") == {
        "repo": "proj", "origin": forge.url, "harness": "claude"
    }

    profile = WorkerProfile(name="xps", host="omarchy-xps13", harnesses=["claude"])
    _, launcher = _drain(tmp_path, service, work_dir, profile=profile)

    assert [cwd for _, cwd in launcher.calls] == [str(work_dir / "proj")]
    synced = service.get_job(job.id)
    assert synced is not None
    assert synced.synced_sha == git(forge.seed, "rev-parse", "HEAD")


# --- a slow sync must not outlive the claim it runs under ---------------------


def test_a_job_reclaimed_by_a_peer_during_a_slow_sync_is_not_launched(
    tmp_path, forge, work_dir
) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, work_dir / "proj", forge.url)
    clock = Clock()

    def slow_sync(job, **kwargs):
        prepared = prepare_repo(job, work_dir=work_dir, **kwargs)
        clock.advance(600)  # far past the 90 s lease
        assert store.reclaim_job(
            job.id, claimed_by="peer", lease_seconds=3600, now=clock()
        ) is not None
        return prepared

    result, launcher = _drain(tmp_path, store, work_dir, clock=clock, preparer=slow_sync)

    assert launcher.calls == []
    assert result.started == 0
    job = store.get_job(job_id)
    assert job is not None and job.claimed_by == "peer"


def test_a_job_reclaimed_during_a_slow_sync_is_not_parked_by_the_scheduler_that_lost_it(
    tmp_path, forge, work_dir
) -> None:
    """Parking a job a peer now runs would strand the peer's run behind `blocked`."""
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, work_dir / "proj", forge.url)
    clock = Clock()

    def slow_then_refused(job, **_kwargs):
        clock.advance(600)  # far past the 90 s lease
        assert store.reclaim_job(
            job.id, claimed_by="peer", lease_seconds=3600, now=clock()
        ) is not None
        raise RepoRefused("could not fast-forward to origin/main in proj: not possible")

    result, launcher = _drain(
        tmp_path, store, work_dir, clock=clock, preparer=slow_then_refused
    )

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by) == ("running", "peer")
    assert (launcher.calls, result.parked) == ([], 0)


def test_a_slow_sync_renews_every_lease_it_held_up(tmp_path, work_dir) -> None:
    """The just-synced job and the job already running both launch under a live lease."""
    forges = {name: Forge(tmp_path / name) for name in ("one", "two")}
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    first = _enqueue(store, work_dir / "one", forges["one"].url)
    second = _enqueue(store, work_dir / "two", forges["two"].url)
    clock = Clock()
    leases: dict[int, datetime] = {}

    def slow_sync(job, **kwargs):
        prepared = prepare_repo(job, work_dir=work_dir, **kwargs)
        if job.id == second:
            clock.advance(600)  # far past the 90 s lease of the job already running
        return prepared

    class LeaseReadingLauncher(FakeLauncher):
        def __call__(self, argv, cwd):
            if len(self.calls) == 1:  # launching the second job
                for job_id in (first, second):
                    lease = store.get_job(job_id).lease_until
                    leases[job_id] = datetime.fromisoformat(lease)
            return super().__call__(argv, cwd)

    _drain(tmp_path, store, work_dir, clock=clock, launcher=LeaseReadingLauncher(),
           preparer=slow_sync)

    assert leases and all(lease > clock() for lease in leases.values()), leases


def test_a_slow_sync_keeps_the_worker_online_and_every_lease_live(tmp_path, work_dir) -> None:
    """A peer looking mid-sync sees a live worker and live leases, so it reclaims nothing.

    Without the keepalive the worker misses its heartbeats, a peer's sweep cuts
    its leases, and the job it just launched — no run attached yet — is
    released and launched a second time.
    """
    forges = {name: Forge(tmp_path / name) for name in ("one", "two")}
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    _enqueue(store, work_dir / "one", forges["one"].url)  # launched first, then held up
    second = _enqueue(store, work_dir / "two", forges["two"].url)
    clock = Clock()
    seen: dict[str, object] = {}

    def slow_sync(job, *, keepalive, **kwargs):
        if job.id == second:
            for _ in range(60):  # ten minutes of a slow clone, beaten in 10 s slices
                clock.advance(10)
                keepalive()
            worker = store.get_worker("xps")
            seen["online"] = worker is not None and worker.is_online(clock())
            seen["reclaimable"] = [lapsed.id for lapsed in store.expired_running_jobs(now=clock())]
        return prepare_repo(job, work_dir=work_dir, keepalive=keepalive, **kwargs)

    _, launcher = _drain(tmp_path, store, work_dir, clock=clock, preparer=slow_sync)

    assert seen == {"online": True, "reclaimable": []}
    assert len(launcher.calls) == 2


def test_a_refused_slow_sync_still_renews_the_leases_it_held_up(tmp_path, work_dir) -> None:
    forges = {name: Forge(tmp_path / name) for name in ("one", "two", "three")}
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    first, second, third = (
        _enqueue(store, work_dir / name, forge.url) for name, forge in forges.items()
    )
    clock = Clock()
    seen: dict[str, datetime] = {}

    def sync(job, **kwargs):
        if job.id == second:
            clock.advance(600)  # far past the 90 s lease of the job already running
            raise RepoRefused("could not fetch origin in two: timed out", retryable=True)
        if job.id == third:
            lease = store.get_job(first).lease_until
            seen["first"], seen["now"] = datetime.fromisoformat(lease), clock()
        return prepare_repo(job, work_dir=work_dir, **kwargs)

    _drain(tmp_path, store, work_dir, clock=clock, preparer=sync)

    assert seen and seen["first"] > seen["now"], seen


def test_a_keepalive_that_cannot_reach_the_store_says_so_and_the_sync_goes_on(
    tmp_path, forge, work_dir
) -> None:
    """A busy `queue.db` mid-sync is reported by the side thread, never raised out of it."""

    class LockedOnce(QueueStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.locked = True

        def renew_lease(self, job_id, **kwargs):
            if self.locked:
                self.locked = False
                raise sqlite3.OperationalError("database is locked")
            return super().renew_lease(job_id, **kwargs)

    store = LockedOnce(tmp_path / "queue.db")
    store.init()
    _enqueue(store, _clone(forge, work_dir / "proj"), forge.url)
    lines: list[str] = []

    def sync(job, *, keepalive, **kwargs):
        keepalive()  # what the side thread does while git runs
        return prepare_repo(job, work_dir=work_dir, keepalive=keepalive, **kwargs)

    _, launcher = _drain(tmp_path, store, work_dir, preparer=sync, echo=lines.append)

    assert any("could not keep the worker alive during its sync" in line for line in lines)
    assert len(launcher.calls) == 1


def test_ctrl_c_during_a_sync_hands_the_claim_back(tmp_path, forge, work_dir) -> None:
    """Ctrl-C hands back every lease the drain holds — the one it is syncing under too."""
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = _enqueue(store, _clone(forge, work_dir / "proj"), forge.url)

    def interrupted(_job, **_kwargs):
        raise KeyboardInterrupt

    result, launcher = _drain(tmp_path, store, work_dir, preparer=interrupted)

    job = store.get_job(job_id)
    assert job is not None
    assert (job.state, job.claimed_by) == ("queued", None)
    assert result.interrupted is True
    assert launcher.calls == []


def test_queue_run_help_describes_the_pre_dispatch_sync() -> None:
    import re

    from typer.testing import CliRunner

    from sdlc.cli import app

    result = CliRunner().invoke(app, ["queue", "run", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
    for needle in ("git merge --ff-only origin/main", "DIRTY_WORKING_TREE",
                   "origin mismatch", "retried after a wait that doubles"):
        assert needle in text, needle
