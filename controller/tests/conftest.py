# ABOUTME: Shared pytest fixtures for the sdlc controller test suite.
# ABOUTME: Mutes real Telegram lifecycle notifications so tests never hit the network.

from __future__ import annotations

import pytest


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
