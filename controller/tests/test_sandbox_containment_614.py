# ABOUTME: Issue #614 — with SDLC_SANDBOX=1 writer stages are contained, not just detected:
# ABOUTME: only a self-contained story clone is mounted, pinned image, writers-only, no fetch.

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from sdlc import dispatch as dispatch_mod
from sdlc.build import (
    BuildOptions,
    Ledger,
    _prepare_story_workdir,
    _syncing_dispatch,
    _teardown_story_workdir,
    create_story_sandbox_clone,
    is_sandbox_clone,
    render_build_prompt,
    sync_sandbox_branch,
)
from sdlc.cohort import Story
from sdlc.dispatch import (
    SANDBOX_ENV,
    SANDBOX_IMAGE_ENV,
    SANDBOXED_ROLES,
    SandboxUnavailableError,
    dispatch_agent,
    pinned_sandbox_image,
    sandbox_wrap,
)
from sdlc.role_routing import bundled_config_path

from test_dispatch import _STREAM_CMD, _sandbox_popen

PINNED = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _repo_with_origin(tmp_path: Path) -> Path:
    """A primary checkout with an ``origin`` (local bare repo — no network)."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True, text=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(origin), str(work)], check=True, capture_output=True, text=True
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: base")
    _git(work, "push", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    _git(work, "fetch", "origin")
    return work


def _story(sid: str = "61.4-001") -> Story:
    return Story(sid, f"Story {sid}", "61", "sandbox", "epic-61.md", "P1", 3, "py", [])


def _ledger(tmp_path: Path, sid: str) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-61", "serial")
    ledger.story_upsert(run_id, sid, "61", "Sandbox", "P1", 3, "py", "", None, "TODO")
    return ledger, run_id


def _mount_sources(argv: list[str]) -> list[Path]:
    return [
        Path(argv[i + 1].split(":", 1)[0]).resolve()
        for i, a in enumerate(argv) if a == "-v"
    ]


@pytest.fixture
def container(monkeypatch):
    """A present runtime, a pinned image, and a captured (never real) launch."""
    monkeypatch.setattr("sdlc.dispatch.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("sdlc.dispatch.pinned_sandbox_image", lambda arch=None: PINNED)
    monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    seen: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _sandbox_popen(seen))
    return seen


# ---------------------------------------------------------------------------
# Regression: the #607 escape is unrepresentable — the primary is never mounted
# ---------------------------------------------------------------------------

@pytest.fixture
def primary_and_clone(tmp_path, monkeypatch):
    """Real git setup; requested *before* ``container`` so Popen is still real."""
    primary = _repo_with_origin(tmp_path)
    monkeypatch.chdir(primary)
    return primary, create_story_sandbox_clone(primary, "61.4-001", "run1-x")


def test_escape_path_absent_from_container(primary_and_clone, container) -> None:
    """#607: `cd /abs/primary && git ...` cannot resolve — neither the primary
    checkout nor its .git is mounted; only the story clone is."""
    primary, clone = primary_and_clone
    dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=clone)

    sources = _mount_sources(container["cmd"])
    assert clone.resolve() in sources
    for src in sources:
        assert src != primary.resolve()
        assert src not in primary.resolve().parents  # no ancestor of it either
        assert src != (primary / ".git").resolve()
    # The clone's git metadata is self-contained: nothing points back into the
    # primary .git, so git works with only the clone mounted.
    assert (clone / ".git").is_dir()
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()
    assert str(primary / ".git") not in (clone / ".git" / "config").read_text()


def test_shared_root_is_refused_not_mounted(tmp_path, monkeypatch, container) -> None:
    """cwd=None means the primary checkout; containing it is refused before launch."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SandboxUnavailableError, match="primary checkout"):
        dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True)
    assert "cmd" not in container


def test_ancestor_of_primary_is_refused(tmp_path, monkeypatch, container) -> None:
    inner = tmp_path / "repo"
    inner.mkdir()
    monkeypatch.chdir(inner)
    with pytest.raises(SandboxUnavailableError):
        dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=tmp_path)


def test_linked_worktree_is_refused(tmp_path, monkeypatch, container) -> None:
    """A linked worktree needs the primary .git to work, which is never mounted."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /abs/primary/.git/worktrees/agent-x\n")
    with pytest.raises(SandboxUnavailableError, match="linked git worktree"):
        dispatch_agent("coverage", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=wt)
    assert "cmd" not in container


# ---------------------------------------------------------------------------
# Scope: writers only
# ---------------------------------------------------------------------------

def test_sandboxed_roles_are_exactly_the_writers() -> None:
    assert SANDBOXED_ROLES == {"build", "coverage", "bugfix"}


@pytest.mark.parametrize("role", ["build", "coverage", "bugfix"])
def test_writer_roles_are_wrapped(role, tmp_path, container) -> None:
    # The canned response is a build result; only the launched argv matters here.
    with contextlib.suppress(Exception):
        dispatch_agent(role, "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=tmp_path)
    assert container["cmd"][:2] == ["podman", "run"]


@pytest.mark.parametrize("role", ["review", "merge", "investigation"])
def test_non_writer_roles_stay_on_host(role, monkeypatch, container) -> None:
    """Review keeps the host deny floor; merge needs forge auth — never wrapped,
    even with the env opt-in and no story clone."""
    monkeypatch.setenv(SANDBOX_ENV, "1")
    with contextlib.suppress(Exception):
        dispatch_agent(role, "p", agent_cmd=_STREAM_CMD)
    assert container["cmd"] == _STREAM_CMD


# ---------------------------------------------------------------------------
# Image: pinned by id per architecture, never a tag, never pulled
# ---------------------------------------------------------------------------

def _pin_file(tmp_path: Path, monkeypatch, body: str) -> None:
    pin = tmp_path / "sandbox-image.yaml"
    pin.write_text(body)
    monkeypatch.setattr(
        "sdlc.role_routing.bundled_config_path",
        lambda name: pin if name == "sandbox-image.yaml" else None,
    )


@pytest.mark.parametrize(
    ("machine", "expected"),
    [("x86_64", "a"), ("amd64", "a"), ("aarch64", "c"), ("arm64", "c")],
)
def test_pinned_image_is_per_arch(machine, expected, tmp_path, monkeypatch) -> None:
    _pin_file(
        tmp_path, monkeypatch,
        f"amd64: sha256:{'a' * 64}\narm64: sha256:{'c' * 64}\n",
    )
    assert pinned_sandbox_image(machine) == "sha256:" + expected * 64


@pytest.mark.parametrize(
    "value", ["", "sdlc-agent-sandbox:latest", "sha256:short", "localhost/x@sha256:" + "a" * 64]
)
def test_non_id_pins_are_unpinned(value, tmp_path, monkeypatch) -> None:
    """A tag (``:latest`` included) or malformed value can never become the default."""
    _pin_file(tmp_path, monkeypatch, f"amd64: {value}\n")
    assert pinned_sandbox_image("x86_64") is None


def test_malformed_pin_file_is_unpinned(tmp_path, monkeypatch) -> None:
    _pin_file(tmp_path, monkeypatch, "amd64: [unclosed\n")
    assert pinned_sandbox_image("x86_64") is None


def test_shipped_pin_file_is_bundled_and_has_no_tag() -> None:
    path = bundled_config_path("sandbox-image.yaml")
    assert path is not None
    assert ":latest" not in path.read_text()


def test_unpinned_host_refuses_to_contain(tmp_path, monkeypatch) -> None:
    """Contain or refuse: no pin for this arch and no override → hard fail."""
    monkeypatch.setattr("sdlc.dispatch.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("sdlc.dispatch.pinned_sandbox_image", lambda arch=None: None)
    monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    with pytest.raises(SandboxUnavailableError, match="deploy.sh"):
        dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=tmp_path)


def test_default_image_is_the_pin_and_never_pulled(tmp_path, container) -> None:
    dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=tmp_path)
    cmd = container["cmd"]
    assert PINNED in cmd
    assert cmd[cmd.index("--pull") + 1] == "never"
    assert cmd[cmd.index("--network") + 1] == "none"


# ---------------------------------------------------------------------------
# Read-only package caches
# ---------------------------------------------------------------------------

def test_existing_host_caches_mount_read_only_offline(tmp_path, monkeypatch, container) -> None:
    uv_cache, npm_cache = tmp_path / "uv", tmp_path / "npm"
    uv_cache.mkdir()
    npm_cache.mkdir()
    monkeypatch.setenv("UV_CACHE_DIR", str(uv_cache))
    monkeypatch.setenv("npm_config_cache", str(npm_cache))
    story = tmp_path / "story"
    story.mkdir()
    dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=story)
    cmd = container["cmd"]
    assert f"{uv_cache}:/cache/uv:ro,z" in cmd
    assert f"{npm_cache}:/cache/npm:ro,z" in cmd
    env = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert {"UV_CACHE_DIR=/cache/uv", "UV_OFFLINE=1",
            "npm_config_cache=/cache/npm", "npm_config_offline=true"} <= set(env)


def test_missing_host_caches_are_not_mounted(tmp_path, monkeypatch, container) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "absent-uv"))
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "absent-npm"))
    story = tmp_path / "story"
    story.mkdir()
    dispatch_agent("build", "p", agent_cmd=_STREAM_CMD, sandbox=True, cwd=story)
    assert _mount_sources(container["cmd"]) == [story.resolve()]
    assert not (tmp_path / "absent-uv").exists()


@pytest.mark.parametrize(
    ("runtime", "keep_id"),
    [("podman", True), ("/usr/bin/podman", True), ("docker", False)],
)
def test_rootless_podman_keeps_host_uid(runtime, keep_id, tmp_path) -> None:
    """Rootless podman needs keep-id or git rejects the mounted clone as
    foreign-owned; docker has no such mode and must not receive the flag."""
    argv = sandbox_wrap(["claude"], runtime=runtime, image="i", mount=tmp_path)
    has = "--userns" in argv and argv[argv.index("--userns") + 1] == "keep-id"
    assert has is keep_id


def test_sandbox_wrap_without_caches_is_unchanged(tmp_path) -> None:
    argv = sandbox_wrap(["claude"], runtime="podman", image="i", mount=tmp_path)
    assert [a for a in argv if a == "-v"] == ["-v"]


# ---------------------------------------------------------------------------
# Story clone lifecycle (real git, offline)
# ---------------------------------------------------------------------------

def test_clone_is_detached_at_fetched_base_with_real_origin(tmp_path) -> None:
    primary = _repo_with_origin(tmp_path)
    clone = create_story_sandbox_clone(primary, "61.4-001", "run1-x")
    assert clone.name == "sandbox-run1-61.4-001"
    assert clone.parent == primary / ".claude" / "worktrees"
    assert is_sandbox_clone(clone)
    # Detached at origin/main; the agent cuts its branch from origin/main unfetched.
    assert _git(clone, "rev-parse", "HEAD").stdout == _git(primary, "rev-parse", "origin/main").stdout
    assert _git(clone, "rev-parse", "--verify", "origin/main").returncode == 0
    # origin keeps the real forge URL for the host-side push/merge stages.
    assert _git(clone, "remote", "get-url", "origin").stdout.strip() == str(tmp_path / "origin.git")
    # Identity is carried in (the container has no ~/.gitconfig).
    assert _git(clone, "config", "user.email").stdout.strip() == "t@example.com"
    # The build agent can branch and commit without any network.
    _git(clone, "checkout", "-b", "feature/61.4-001", "origin/main")


def test_clone_reattaches_on_resume(tmp_path) -> None:
    primary = _repo_with_origin(tmp_path)
    clone = create_story_sandbox_clone(primary, "61.4-001", "run1-x")
    _git(clone, "checkout", "-b", "feature/61.4-001")
    (clone / "work.txt").write_text("in flight\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "feat: in flight")
    again = create_story_sandbox_clone(primary, "61.4-001", "run1-x")
    assert again == clone
    assert (again / "work.txt").exists()


def test_clone_failure_raises_and_leaves_nothing(tmp_path) -> None:
    from sdlc.build import WorktreeError

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError):
        create_story_sandbox_clone(not_a_repo, "61.4-001", "run1-x")
    assert not (not_a_repo / ".claude" / "worktrees" / "sandbox-run1-61.4-001").exists()


def test_sync_fetches_story_branch_back_to_primary(tmp_path) -> None:
    primary = _repo_with_origin(tmp_path)
    clone = create_story_sandbox_clone(primary, "61.4-001", "run1-x")
    assert sync_sandbox_branch(primary, clone, "61.4-001") is True  # nothing yet: no-op
    _git(clone, "checkout", "-b", "feature/61.4-001")
    (clone / "f.txt").write_text("x\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "feat: x")
    assert sync_sandbox_branch(primary, clone, "61.4-001") is True
    assert (
        _git(primary, "rev-parse", "feature/61.4-001").stdout
        == _git(clone, "rev-parse", "HEAD").stdout
    )


def test_syncing_dispatch_fetches_back_even_when_dispatch_raises(tmp_path) -> None:
    primary = _repo_with_origin(tmp_path)
    clone = create_story_sandbox_clone(primary, "61.4-001", "run1-x")

    def agent(*args, **kwargs):
        _git(clone, "checkout", "-b", "feature/61.4-001")
        _git(clone, "commit", "--allow-empty", "-m", "feat: y")
        raise RuntimeError("stage blew up")

    with pytest.raises(RuntimeError):
        _syncing_dispatch(agent, primary, clone, "61.4-001")("build", "p")
    assert _git(primary, "rev-parse", "--verify", "feature/61.4-001").returncode == 0


def test_prepare_workdir_clones_even_when_serial(tmp_path, monkeypatch) -> None:
    """Contain at any concurrency: a serial sandboxed story never uses the root."""
    primary = _repo_with_origin(tmp_path)
    monkeypatch.chdir(primary)
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    workdir = _prepare_story_workdir(
        BuildOptions(sequential=True, sandbox=True), _story(), ledger, run_id, real_run=True
    )
    assert workdir is not None and is_sandbox_clone(workdir)
    assert ledger.story_worktree(run_id, "61.4-001") == str(workdir)


def test_prepare_workdir_honours_env_opt_in(tmp_path, monkeypatch) -> None:
    primary = _repo_with_origin(tmp_path)
    monkeypatch.chdir(primary)
    monkeypatch.setenv(SANDBOX_ENV, "1")
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    workdir = _prepare_story_workdir(
        BuildOptions(sequential=True), _story(), ledger, run_id, real_run=True
    )
    assert is_sandbox_clone(workdir)


def test_prepare_workdir_clone_failure_is_loud_not_a_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # not a repo: fetch and clone both fail
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    workdir = _prepare_story_workdir(
        BuildOptions(sandbox=True), _story(), ledger, run_id, real_run=True
    )
    assert workdir is None  # dispatch then refuses to mount the primary
    assert any(
        e["level"] == "error" and "no host fallback" not in e["message"]
        and "sandbox clone unavailable" in e["message"]
        for e in ledger.recent_events(run_id, limit=50)
    )


def test_prepare_workdir_fake_dispatcher_run_touches_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    assert _prepare_story_workdir(
        BuildOptions(sandbox=True), _story(), ledger, run_id, real_run=False
    ) is None


def test_teardown_syncs_then_removes_clone(tmp_path, monkeypatch) -> None:
    primary = _repo_with_origin(tmp_path)
    monkeypatch.chdir(primary)
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    clone = create_story_sandbox_clone(primary, "61.4-001", run_id)
    ledger.set_story_worktree(run_id, "61.4-001", str(clone))
    _git(clone, "checkout", "-b", "feature/61.4-001")
    _git(clone, "commit", "--allow-empty", "-m", "feat: unpushed")
    _teardown_story_workdir(ledger, run_id, "61.4-001", real_run=True)
    assert not clone.exists()
    assert _git(primary, "rev-parse", "--verify", "feature/61.4-001").returncode == 0


def test_teardown_keeps_clone_when_sync_fails(tmp_path, monkeypatch) -> None:
    """Never lose unpushed commits: a branch that cannot be fetched back (the
    operator has it checked out) keeps the clone on disk."""
    primary = _repo_with_origin(tmp_path)
    monkeypatch.chdir(primary)
    ledger, run_id = _ledger(tmp_path, "61.4-001")
    clone = create_story_sandbox_clone(primary, "61.4-001", run_id)
    ledger.set_story_worktree(run_id, "61.4-001", str(clone))
    _git(primary, "checkout", "-b", "feature/61.4-001")
    _git(clone, "checkout", "-b", "feature/61.4-001")
    _git(clone, "commit", "--allow-empty", "-m", "feat: diverged")
    _teardown_story_workdir(ledger, run_id, "61.4-001", real_run=True)
    assert clone.exists()


# ---------------------------------------------------------------------------
# Prompt: a contained build never fetches
# ---------------------------------------------------------------------------

def test_sandboxed_build_prompt_does_not_fetch() -> None:
    prompt = render_build_prompt(_story(), BuildOptions(sandbox=True))
    assert "git fetch origin" not in prompt
    assert "git checkout -b feature/61.4-001 origin/main" in prompt
    assert "network-less sandbox" in prompt


def test_host_build_prompt_still_fetches(monkeypatch) -> None:
    monkeypatch.delenv(SANDBOX_ENV, raising=False)
    prompt = render_build_prompt(_story(), BuildOptions())
    assert "git fetch origin && git checkout -b feature/61.4-001 origin/main" in prompt
    assert "network-less sandbox" not in prompt


def test_module_exposes_no_latest_default() -> None:
    assert not hasattr(dispatch_mod, "DEFAULT_SANDBOX_IMAGE")
