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
