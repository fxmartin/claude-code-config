# ABOUTME: Tests for a fleet worker's repo auto-sync before dispatch (Story 35.2-002).
# ABOUTME: Real bare-origin fixtures: fast-forward, clone-if-absent, dirty refusal, origin mismatch.

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.queue_worker import (
    PreparedRepo,
    RepoRefused,
    WorkerProfile,
    prepare_repo,
    repo_origin,
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
    """A bare ``origin`` plus the seed clone that advances its ``main``."""

    def __init__(self, root: Path) -> None:
        self.bare = root / "forge" / "proj.git"
        self.bare.mkdir(parents=True)
        git(self.bare, "init", "-q", "--bare", "-b", "main")
        self.seed = root / "seed"
        git(root, "clone", "-q", str(self.bare), str(self.seed))
        git(self.seed, "checkout", "-q", "-b", "main")
        commit(self.seed, "README.md")
        git(self.seed, "push", "-q", "origin", "main")

    @property
    def url(self) -> str:
        return str(self.bare)

    def advance(self, name: str = "next.txt") -> str:
        sha = commit(self.seed, name)
        git(self.seed, "push", "-q", "origin", "main")
        return sha


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
    job = _job(tmp_path, work_dir / "proj", str(tmp_path / "no-such-forge.git"))

    with pytest.raises(RepoRefused) as refusal:
        prepare_repo(job, work_dir=work_dir)

    assert "clone" in refusal.value.reason
    assert not (work_dir / "proj").exists()


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


def test_repo_origin_reads_the_origin_url_and_none_when_there_is_none(
    tmp_path, forge, work_dir
) -> None:
    clone = _clone(forge, work_dir / "proj")
    assert repo_origin(clone) == forge.url
    assert repo_origin(tmp_path / "missing") is None
    git(tmp_path, "init", "-q", "bare-less")
    assert repo_origin(tmp_path / "bare-less") is None


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
    with pytest.raises(RepoRefused, match="git fetch timed out") as refusal:
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
    with pytest.raises(RepoRefused, match="timed out") as refusal:
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


def test_origin_requirements_records_the_origin_json_or_none(tmp_path, forge, work_dir) -> None:
    from sdlc.queue_worker import origin_requirements

    clone = _clone(forge, work_dir / "proj")
    assert json.loads(origin_requirements(clone)) == {"origin": forge.url}
    assert origin_requirements(tmp_path / "missing") is None


def test_repo_origin_is_none_when_git_cannot_run(tmp_path, monkeypatch) -> None:
    from sdlc import queue_worker

    def boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(queue_worker.subprocess, "run", boom)
    assert repo_origin(tmp_path) is None


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
    registry=None, preparer=None, **kwargs,
):
    clock = clock or Clock()
    launcher = launcher or FakeLauncher()
    profile = WorkerProfile(name="xps", host="omarchy-xps13") if worker else None
    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, worker=profile),
        registry=registry or Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
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


def test_queue_run_help_describes_the_pre_dispatch_sync() -> None:
    import re

    from typer.testing import CliRunner

    from sdlc.cli import app

    result = CliRunner().invoke(app, ["queue", "run", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
    for needle in ("git merge --ff-only origin/main", "DIRTY_WORKING_TREE",
                   "origin mismatch"):
        assert needle in text, needle
