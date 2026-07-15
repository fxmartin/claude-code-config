# ABOUTME: Shared pytest fixtures for the sdlc controller test suite.
# ABOUTME: Mutes real Telegram lifecycle notifications so tests never hit the network.

from __future__ import annotations

import pytest


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
