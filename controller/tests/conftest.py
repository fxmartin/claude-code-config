# ABOUTME: Shared pytest fixtures for the sdlc controller test suite.
# ABOUTME: Mutes real Telegram sends and host auto-detection so tests never hit the network.

from __future__ import annotations

import pytest

from sdlc import fix_issue, issue_host


@pytest.fixture(autouse=True)
def _mute_coverage_precheck(monkeypatch):
    """Make the coverage pre-check inconclusive for every test by default.

    The pre-check (Story 27.3-001) resolves the *current working directory's*
    test command on the sequential path — under pytest that is the controller
    repo itself, so an un-stubbed pre-check would recursively run this very
    suite inside any test that drives ``run_build`` through the coverage
    stage. ``None`` (inconclusive) reproduces the pre-27.3-001 dispatch
    behavior byte-for-byte; the gate's own tests override this explicitly.
    """
    import sdlc.coverage_precheck as precheck_mod

    monkeypatch.setattr(
        precheck_mod, "run_precheck", lambda root, base_ref, branch, timeout=600: None
    )


@pytest.fixture(autouse=True)
def _mute_lifecycle_notifications(monkeypatch):
    """Disable real Telegram sends for every test by default.

    Production call sites in build.py / resume.py invoke ``sdlc.notify.notify``,
    which falls back to ``~/.claude/config/.env`` for credentials. On a developer
    machine those creds exist, so an un-muted suite would POST real messages.
    Setting ``SDLC_NOTIFY=off`` makes ``notify`` a guaranteed no-op. The notifier's
    own tests (test_notify.py) re-enable it explicitly via their own fixture.
    """
    monkeypatch.setenv("SDLC_NOTIFY", "off")


@pytest.fixture(autouse=True)
def _no_real_host_cli(monkeypatch):
    """Block real ``gh``/``glab`` invocations for every test by default.

    Under pytest the cwd is this real repository, so any dispatch-loop path
    that resolves a host adapter from the origin remote (e.g. the Story
    27.3-003 review-packet bake) would otherwise shell out to real
    ``gh pr view``/``gh pr diff`` network calls per test — the fake PR numbers
    tests use (7, 100, …) exist in this repo, so the calls even succeed. That
    slowed the suite from ~130s to 30+ minutes and tripped the dispatcher's
    300s stall watchdog. ``_default_runner`` is the adapters' designed test
    seam ("the host call is the single seam to stub"); raising from it lands
    every host-touching call site on its best-effort fallback. Local ``git``
    detection (``_remote_url``) stays real — it never leaves the machine.
    Tests that exercise adapters inject a fake runner explicitly;
    test_issue_host.py overrides this fixture to test the real runner.
    """

    def _blocked(argv, timeout=None):
        raise issue_host.IssueHostError(
            f"hermetic test suite: refusing to run {argv[0]!r} — inject a fake runner"
        )

    monkeypatch.setattr(issue_host, "_default_runner", _blocked)
    # fix_issue imports the runner by value, so its module global needs the
    # same stub for its `runner or _default_runner` defaults.
    monkeypatch.setattr(fix_issue, "_default_runner", _blocked)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    """Point the host-level registry at a per-test tmp file (issue #556).

    Real-run code paths (``dispatcher=None``) in ``run_fix``/``run_build``/
    ``run_fix_batch`` instantiate ``Registry()`` with no explicit path, which
    resolves ``default_registry_path()`` — ``~/.sdlc/registry.json`` on a dev
    machine when ``SDLC_REGISTRY_PATH`` is unset. Under pytest that env var is
    unset unless a test sets it explicitly, so any test exercising a real-run
    path (e.g. ``test_batch_real_run_isolates_worktrees_and_captures_worker_exception``)
    wrote its fake run into the developer's real host registry, surfacing as a
    FAILED project card on the dashboard. ``SDLC_REGISTRY_PATH`` is the
    registry's designed test seam (registry.py); per-test
    ``monkeypatch.setenv``/``delenv`` calls in test_cli_runs.py, test_registry.py,
    and test_runlog.py run after fixture setup and override this default.
    """
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))


@pytest.fixture(autouse=True)
def _no_real_git_push(monkeypatch):
    """Block a real ``git push`` for every test by default (issue #527).

    The bugfix loop publishes its commit with ``build._git_push`` before
    retrying the stage. On the sequential path the push root is ``workdir or
    Path.cwd()`` — and under pytest that cwd is *this* repository, where story
    ids the suite invents (28.1-002, …) collide with real local
    ``feature/<id>`` branches left by earlier `sdlc build` runs. An un-stubbed
    push therefore reaches the network, and on a checkout whose matching branch
    carries unpushed commits it would publish them to origin: running the test
    suite must never write to a remote. Same rationale, and same seam-stubbing
    shape, as :func:`_no_real_host_cli` above.

    Returns a *successful* CompletedProcess so the block is transparent — the
    loop proceeds exactly as it does when a real push lands, rather than being
    diverted into the park-on-push-failure branch. ``_push_bugfix_commit``'s own
    tests monkeypatch this same seam explicitly, which overrides the fixture.
    """
    import subprocess

    import sdlc.build as build_mod

    def _blocked_push(root, branch):
        return subprocess.CompletedProcess(
            ["git", "push", "origin", branch], 0, "", ""
        )

    monkeypatch.setattr(build_mod, "_git_push", _blocked_push)
