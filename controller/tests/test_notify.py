# ABOUTME: Tests for the best-effort Telegram lifecycle notifier (sdlc.notify).
# ABOUTME: Inject a fake sender; assert payload shape, no-op gating, and never-raises.

from __future__ import annotations

import json

import pytest

from sdlc import notify as notify_mod


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Give every test a clean, configured environment by default.

    Individual tests override pieces (clear a credential, flip SDLC_NOTIFY) to
    exercise the gating paths. Pre-seeding credentials here means the default
    path is the "configured" one, so the no-op tests are explicit about *why*
    they no-op.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat456")
    monkeypatch.delenv("SDLC_NOTIFY", raising=False)


def _collector():
    """A fake sender that accumulates (title, body) tuples instead of POSTing."""
    sent: list[tuple[str, str]] = []

    def sender(url: str, payload: bytes) -> None:  # urllib-shaped signature
        sent.append((url, payload))

    return sent, sender


# --- payload shape / escaping -------------------------------------------------


def test_notify_run_started_builds_json_payload(monkeypatch):
    captured: list[tuple[str, bytes]] = []

    def sender(url: str, payload: bytes) -> None:
        captured.append((url, payload))

    notify_mod.notify("run_started", scope="epic-09", mode="auto", sender=sender)

    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "https://api.telegram.org/bottok123/sendMessage"
    data = json.loads(payload.decode("utf-8"))
    assert data["chat_id"] == "chat456"
    assert "epic-09" in data["text"]
    assert "auto" in data["text"]
    # Title is human-readable, not the raw event slug.
    assert "run_started" not in data["text"]


def test_notify_escapes_special_characters(monkeypatch):
    captured: list[bytes] = []

    def sender(url: str, payload: bytes) -> None:
        captured.append(payload)

    # Quotes, backslashes, newlines and markdown chars must round-trip via JSON,
    # never break the payload. This is why we build with json.dumps, not f-strings.
    nasty = 'scope "x"\n\\back* _md_'
    notify_mod.notify("run_finished", terminal="FAILED", detail=nasty, sender=sender)

    data = json.loads(captured[0].decode("utf-8"))
    assert nasty in data["text"]


def test_notify_run_finished_includes_terminal_and_tally(monkeypatch):
    captured: list[bytes] = []
    monkeypatch.setattr(
        notify_mod, "_send_telegram",
        lambda title, body, *, sender=None: captured.append((title, body)),
    )

    notify_mod.notify(
        "run_finished",
        terminal="DONE",
        done=3,
        failed=0,
        blocked=1,
    )
    title, body = captured[0]
    assert "DONE" in (title + body)
    assert "done=3" in body or "3" in body
    assert "blocked=1" in body or "blocked" in body


def test_notify_rate_limited_includes_reset(monkeypatch):
    captured: list[bytes] = []

    def sender(url: str, payload: bytes) -> None:
        captured.append(payload)

    notify_mod.notify("rate_limited", reset_at=1700000000, sender=sender)
    data = json.loads(captured[0].decode("utf-8"))
    assert "1700000000" in data["text"]


def test_notify_story_failed_includes_story_id(monkeypatch):
    captured: list[bytes] = []

    def sender(url: str, payload: bytes) -> None:
        captured.append(payload)

    notify_mod.notify("story_failed", story_id="09.1-004", sender=sender)
    data = json.loads(captured[0].decode("utf-8"))
    assert "09.1-004" in data["text"]


# --- no-op gating -------------------------------------------------------------


def test_no_op_when_token_absent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # Force the env-only path: an empty .env candidate so no fallback creds load.
    monkeypatch.setattr(notify_mod, "_load_creds", lambda: (None, "chat456"))
    sent, sender = _collector()
    notify_mod.notify("run_started", scope="x", sender=sender)
    assert sent == []


def test_no_op_when_chat_absent(monkeypatch):
    monkeypatch.setattr(notify_mod, "_load_creds", lambda: ("tok", None))
    sent, sender = _collector()
    notify_mod.notify("run_started", scope="x", sender=sender)
    assert sent == []


def test_no_op_when_sdlc_notify_off(monkeypatch):
    monkeypatch.setenv("SDLC_NOTIFY", "off")
    sent, sender = _collector()
    notify_mod.notify("run_started", scope="x", sender=sender)
    assert sent == []


def test_no_op_when_sdlc_notify_false(monkeypatch):
    monkeypatch.setenv("SDLC_NOTIFY", "false")
    sent, sender = _collector()
    notify_mod.notify("run_started", scope="x", sender=sender)
    assert sent == []


def test_sends_when_sdlc_notify_on(monkeypatch):
    monkeypatch.setenv("SDLC_NOTIFY", "on")
    sent, sender = _collector()
    notify_mod.notify("run_started", scope="x", sender=sender)
    assert len(sent) == 1


# --- never raises -------------------------------------------------------------


def test_notify_never_raises_when_sender_throws(monkeypatch):
    def boom(url: str, payload: bytes) -> None:
        raise RuntimeError("network down")

    # Must not propagate — a broken notifier can never fail a build.
    notify_mod.notify("run_started", scope="x", sender=boom)


def test_notify_never_raises_when_creds_loader_throws(monkeypatch):
    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(notify_mod, "_load_creds", boom)
    notify_mod.notify("run_started", scope="x")


def test_notify_never_raises_on_unknown_event(monkeypatch):
    sent, sender = _collector()
    # Unknown events still format generically, never raise.
    notify_mod.notify("totally_unknown_event", foo="bar", sender=sender)
    assert len(sent) == 1


# --- .env fallback parsing ----------------------------------------------------


def test_load_creds_prefers_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "envchat")
    token, chat = notify_mod._load_creds()
    assert token == "envtok"
    assert chat == "envchat"


def test_load_creds_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "TELEGRAM_BOT_TOKEN=filetok\n"
        'TELEGRAM_CHAT_ID="filechat"\n'
        "\n"
        "export OTHER=ignored\n"
    )
    monkeypatch.setattr(notify_mod, "_ENV_FILE", env_file)
    token, chat = notify_mod._load_creds()
    assert token == "filetok"
    assert chat == "filechat"


def test_load_creds_env_file_missing_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify_mod, "_ENV_FILE", tmp_path / "does-not-exist.env")
    token, chat = notify_mod._load_creds()
    assert token is None
    assert chat is None


def test_load_creds_strips_export_and_quotes(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "export TELEGRAM_BOT_TOKEN='singlequoted'\n"
        "TELEGRAM_CHAT_ID = spacedvalue \n"
    )
    monkeypatch.setattr(notify_mod, "_ENV_FILE", env_file)
    token, chat = notify_mod._load_creds()
    assert token == "singlequoted"
    assert chat == "spacedvalue"
