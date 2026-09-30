# ABOUTME: Live model entitlement probe with previous-generation fallback (Story 34.1-002).
# ABOUTME: Proves each tier id works on this host, caches 24h per host, degrades to TIER_FALLBACK_IDS.

from __future__ import annotations

import json
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sdlc.model_routing import TIER_FALLBACK_IDS, TIER_MODEL_IDS, set_probe_state
from sdlc.rate_limit import detect_rate_limit

CACHE_FILENAME = "model-probe.json"
CACHE_TTL = timedelta(hours=24)
_PROBE_TIMEOUT_SECONDS = 60

OK = "ok"
ENTITLEMENT = "entitlement"
INCONCLUSIVE = "inconclusive"

# Wording the claude CLI / API uses when an id does not exist or the plan lacks it.
_ENTITLEMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"not_found_error", re.I),
    re.compile(r"model[^\n]{0,80}\bnot found\b", re.I),
    re.compile(r"\bnot found\b[^\n]{0,40}\bmodel", re.I),
    re.compile(r"issue with the selected model", re.I),
    re.compile(r"(?:may not exist|does not exist)", re.I),
    re.compile(r"(?:do(?:es)? not|don't) have access", re.I),
    re.compile(r"\bnot (?:available|entitled|authori[sz]ed)\b[^\n]{0,40}\bmodel", re.I),
    re.compile(r"\binvalid model\b", re.I),
)

ProbeRunner = Callable[..., "tuple[int, str]"]


def classify_probe(returncode: int, detail: str) -> str:
    """Read one probe result: ``ok``, ``entitlement`` or ``inconclusive``.

    A rate limit (Story 14.1-003 wording) is checked first and is never an
    entitlement failure — the RATE_LIMITED path owns it. Timeouts, a missing
    CLI and unrecognised errors are inconclusive: keep the current id.
    """
    if returncode == 0:
        return OK
    if detect_rate_limit(detail) is not None:
        return INCONCLUSIVE
    if any(p.search(detail or "") for p in _ENTITLEMENT_PATTERNS):
        return ENTITLEMENT
    return INCONCLUSIVE


def _default_runner(argv: list[str], timeout_s: int = _PROBE_TIMEOUT_SECONDS) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        return 127, f"command not found: {argv[0] if argv else ''}"
    except subprocess.TimeoutExpired:
        return 124, "probe command timed out"
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()


def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "sdlc"


@dataclass
class ProbeOutcome:
    """Per-id status (``ok``/``entitlement``/``inconclusive``) and the fallout."""

    statuses: dict[str, str] = field(default_factory=dict)
    substitutions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cached: bool = False


def _load_cache(path: Path, host: str, now: datetime) -> dict[str, str]:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))[host]
        at = datetime.fromisoformat(entry["at"])
        statuses = dict(entry["statuses"])
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not (timedelta(0) <= now - at < CACHE_TTL):
        return {}
    return statuses


def _store_cache(path: Path, host: str, now: datetime, statuses: dict[str, str]) -> None:
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[host] = {"at": now.isoformat(), "statuses": statuses}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written only costs the next run a probe


def gather_statuses(
    *,
    runner: ProbeRunner | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    host: str | None = None,
) -> tuple[dict[str, str], bool]:
    """Probe (or load the 24h cached) status of every tier id; ``(statuses, cached)``."""
    now = now or datetime.now(timezone.utc)
    host = host or socket.gethostname()
    cache_path = (state_dir or default_state_dir()) / CACHE_FILENAME
    ids = list(dict.fromkeys(TIER_MODEL_IDS.values()))
    cached = _load_cache(cache_path, host, now)
    if all(i in cached for i in ids):
        return {i: cached[i] for i in ids}, True
    run = runner or _default_runner
    statuses: dict[str, str] = {}
    for model_id in ids:
        try:
            code, detail = run(["claude", "-p", "ok", "--model", model_id])
            statuses[model_id] = classify_probe(code, detail)
        except Exception:  # noqa: BLE001 - a broken probe must never abort a run
            statuses[model_id] = INCONCLUSIVE
    # Inconclusive results (rate limit, offline) are not worth remembering.
    if INCONCLUSIVE not in statuses.values():
        _store_cache(cache_path, host, now, statuses)
    return statuses, False


def probe_tier_models(
    *,
    runner: ProbeRunner | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    host: str | None = None,
) -> ProbeOutcome:
    """Prove every tier id on this host and install the outcome for dispatch.

    An ``entitlement`` failure substitutes the previous-generation id (with a
    warning); ``ok`` ids become eligible for ``--fallback-model``.
    """
    statuses, cached = gather_statuses(runner=runner, state_dir=state_dir, now=now, host=host)
    outcome = ProbeOutcome(statuses=statuses, cached=cached)
    for model_id, status in statuses.items():
        fallback = TIER_FALLBACK_IDS.get(model_id)
        if status == ENTITLEMENT and fallback and fallback != model_id:
            outcome.substitutions[model_id] = fallback
            outcome.warnings.append(
                f"model probe: {model_id} is not available on this host — "
                f"substituting {fallback}"
            )
    verified = {i for i, s in statuses.items() if s == OK}
    set_probe_state(outcome.substitutions, verified)
    return outcome
