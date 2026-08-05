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

# Terminal run status → emoji, for the run_finished title. Unrecognised (or
# absent) terminals fall back to a neutral checkered flag.
_TERMINAL_EMOJI = {
    "DONE": "✅",
    "FAILED": "❌",
    "ABORTED": "⚠️",
    "NEEDS_ATTENTION": "⚠️",
    "AWAITING_APPROVAL": "⏳",
    "RATE_LIMITED": "⏸",
}

# Count fields folded into the run_finished tally line, in display order.
_COUNT_KEYS = ("done", "failed", "blocked", "needs_attention", "awaiting_approval", "skipped")

_SUBJECT_MAX = 80


def _truncate(text: object, limit: int = _SUBJECT_MAX) -> str:
    """Shorten ``text`` to ``limit`` chars (ellipsis) so one message stays one line."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _repo_prefix(fields: dict[str, object]) -> str:
    repo = fields.get("repo")
    return f"{repo} — " if repo else ""


def _subject(fields: dict[str, object]) -> str | None:
    """The human label for a run: ``subject`` (rich) or ``scope`` (legacy id)."""
    subject = fields.get("subject") or fields.get("scope")
    return _truncate(subject) if subject else None


def _run_started_title(fields: dict[str, object]) -> str:
    label = _subject(fields) or "run"
    tail = fields.get("detail") or fields.get("mode")
    if tail:
        label = f"{label} ({tail})"
    return f"🚀 {_repo_prefix(fields)}{label}"


def _run_finished_title(fields: dict[str, object]) -> str:
    terminal = fields.get("terminal")
    emoji = _TERMINAL_EMOJI.get(str(terminal), "🏁")
    core = _subject(fields) or "run"
    if terminal:
        core = f"{core} {terminal}"
    pr = fields.get("pr")
    done, total = fields.get("done"), fields.get("total")
    if pr is not None:
        core = f"{core} → PR #{pr}"
    elif done is not None and total is not None:
        core = f"{core}: {done}/{total} merged"
    duration = fields.get("duration")
    if duration:
        core = f"{core} ({duration})"
    return f"{emoji} {_repo_prefix(fields)}{core}"


def _rate_limited_title(fields: dict[str, object]) -> str:
    label = _subject(fields)
    suffix = f" ({label})" if label else ""
    return f"⏸ {_repo_prefix(fields)}run parked (rate-limited){suffix}"


def _story_failed_title(fields: dict[str, object]) -> str:
    story_id = fields.get("story_id")
    subject = fields.get("subject")
    if story_id and subject:
        label = f'story {story_id} "{_truncate(subject)}"'
    elif story_id:
        label = f"story {story_id}"
    else:
        label = "story"
    return f"❌ {_repo_prefix(fields)}{label} FAILED"


# Per-event formatters producing the rich title. Events not listed here (and
# any formatter that raises) fall back to the legacy generic title/body.
_FORMATTERS: dict[str, Callable[[dict[str, object]], str]] = {
    "run_started": _run_started_title,
    "run_finished": _run_finished_title,
    "rate_limited": _rate_limited_title,
    "story_failed": _story_failed_title,
}

# Fields each formatter already renders into the title, so the generic body
# builder does not repeat them as raw ``key=value`` pairs.
_TITLE_FIELDS: dict[str, set[str]] = {
    "run_started": {"repo", "subject", "scope", "detail", "mode"},
    "run_finished": {"repo", "subject", "scope", "terminal", "pr", "done", "total", "duration"},
    "rate_limited": {"repo", "subject", "scope"},
    "story_failed": {"repo", "subject", "story_id"},
}


def _rich_body(event: str, fields: dict[str, object]) -> str:
    """Body for a formatted event: count tally, leftover fields, run id.

    Any field the title formatter did not consume degrades to the legacy
    ``key=value`` rendering here, so a call site passing extra or unexpected
    fields (or omitting the new ones entirely) can never lose information or
    crash the notifier.
    """
    lines = []
    tally = " ".join(f"{key}={fields[key]}" for key in _COUNT_KEYS if key in fields)
    if tally:
        lines.append(tally)
    consumed = _TITLE_FIELDS.get(event, set()) | set(_COUNT_KEYS) | {"run"}
    leftover = " ".join(
        f"{key}={value}" for key, value in fields.items() if key not in consumed
    )
    if leftover:
        lines.append(leftover)
    run_id = fields.get("run")
    if run_id:
        lines.append(f"run {str(run_id)[:8]}")
    return "\n".join(lines)


def _enabled() -> bool:
    """Whether notifications are on. ``off``/``false`` mute; default is on."""
    return os.environ.get("SDLC_NOTIFY", "on").strip().lower() not in {"off", "false"}


def notify(event: str, *, sender: Sender | None = None, **fields: object) -> None:
    """Emit a best-effort lifecycle notification. Never raises.

    Known events (``run_started``, ``run_finished``, ``rate_limited``,
    ``story_failed``) render a human-readable one-line title from whichever
    optional structured fields (``repo``, ``subject``, ``detail``, ``pr``,
    ``duration``, ...) the call site supplied; missing ones are simply omitted.
    Unknown events, and any formatter failure, fall back to the legacy
    title-cased slug plus a generic ``key=value`` body — so a new or malformed
    call site can never crash the notifier. No-ops when muted (``SDLC_NOTIFY``
    off/false) or when credentials are absent. ``sender`` is injectable for
    tests.
    """
    try:
        if not _enabled():
            return
        formatter = _FORMATTERS.get(event)
        title: str | None = None
        if formatter is not None:
            try:
                title = formatter(fields)
            except Exception:
                title = None
        if title is not None:
            body = _rich_body(event, fields)
        else:
            title = _TITLES.get(event, event.replace("_", " ").title())
            body = " ".join(f"{key}={value}" for key, value in fields.items())
        _send_telegram(title, body, sender=sender)
    except Exception:
        # Belt-and-suspenders: a broken notifier can never fail a build.
        pass
