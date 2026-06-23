# ABOUTME: Best-effort Telegram lifecycle notifications for the sdlc controller.
# ABOUTME: Stdlib-only, never raises, silent no-op when unconfigured or disabled.

"""Deterministic run-lifecycle notifications, decoupled from cmux.

Phase 1 of retiring cmux: the controller emits Telegram messages directly at
run lifecycle transitions (run started / finished / rate-limited / story failed)
instead of relying on cmux hooks. Every public entry point is best-effort — it
swallows all errors so a broken or unreachable notifier can never fail a build.

Configuration:
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  — credentials (env first, then
        ``~/.claude/config/.env`` as a fallback, mirroring cmux-bridge.sh).
    SDLC_NOTIFY                            — set to ``off``/``false`` to mute
        (anything else, or unset, means on).

Stdlib only (os, json, urllib.request); no new dependencies.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Callable

# The .env fallback location, matching where install.sh / the cmux bridge keep
# the shared bot credentials. Patched in tests.
_ENV_FILE = Path.home() / ".claude" / "config" / ".env"

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# A sender takes (url, payload-bytes) and performs the POST. Injectable for tests.
Sender = Callable[[str, bytes], None]


def _load_creds() -> tuple[str | None, str | None]:
    """Resolve the bot token and chat id.

    Environment variables win; otherwise fall back to a tiny stdlib parse of
    ``~/.claude/config/.env`` (no python-dotenv). Missing file or unreadable
    values yield ``None`` so the caller no-ops cleanly.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat

    file_vals: dict[str, str] = {}
    try:
        if _ENV_FILE.is_file():
            for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    file_vals[key] = value
    except OSError:
        # Unreadable .env is not fatal — treat as unconfigured.
        return token, chat

    token = token or file_vals.get("TELEGRAM_BOT_TOKEN")
    chat = chat or file_vals.get("TELEGRAM_CHAT_ID")
    return token, chat


def _default_sender(url: str, payload: bytes) -> None:
    """POST ``payload`` to ``url`` via urllib; swallow network errors.

    Non-blocking in spirit: a short timeout keeps a slow/unreachable Telegram
    from stalling the build, and any failure is silently dropped (the caller
    wraps this in its own guard too).
    """
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        # A failed send must never surface to the caller.
        pass


def _send_telegram(title: str, body: str, *, sender: Sender | None = None) -> None:
    """Send ``title``/``body`` to Telegram, or no-op when unconfigured.

    The payload is built with ``json.dumps`` so quotes, backslashes, newlines
    and markdown characters round-trip safely — never string-interpolated.
    """
    token, chat = _load_creds()
    if not token or not chat:
        return
    text = f"[sdlc] {title}\n{body}" if body else f"[sdlc] {title}"
    payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    url = _TELEGRAM_API.format(token=token)
    (sender or _default_sender)(url, payload)


# Human-readable titles per lifecycle event. Unknown events fall back to a
# title-cased slug so a new call site never crashes the notifier.
_TITLES = {
    "run_started": "Run started",
    "run_finished": "Run finished",
    "rate_limited": "Run rate-limited (parked)",
    "story_failed": "Story failed",
}


def _enabled() -> bool:
    """Whether notifications are on. ``off``/``false`` mute; default is on."""
    return os.environ.get("SDLC_NOTIFY", "on").strip().lower() not in {"off", "false"}


def notify(event: str, *, sender: Sender | None = None, **fields: object) -> None:
    """Emit a best-effort lifecycle notification. Never raises.

    ``event`` selects the title; ``fields`` are formatted into the body as
    ``key=value`` pairs. No-ops when muted (``SDLC_NOTIFY`` off/false) or when
    credentials are absent. ``sender`` is injectable for tests.
    """
    try:
        if not _enabled():
            return
        title = _TITLES.get(event, event.replace("_", " ").title())
        body = " ".join(f"{key}={value}" for key, value in fields.items())
        _send_telegram(title, body, sender=sender)
    except Exception:
        # Belt-and-suspenders: a broken notifier can never fail a build.
        pass
