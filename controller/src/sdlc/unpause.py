# ABOUTME: Story 32.2-003 — operator-declared limit reset for `sdlc queue unpause`.
# ABOUTME: Clears the shared queue pause and re-arms every run parked RATE_LIMITED.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sdlc.queue import QueuePause, QueueStore
from sdlc.registry import Registry, pid_alive

_RATE_LIMITED = "RATE_LIMITED"
# What a re-armed run/story goes back to: the same status `run_resume` stamps
# before it dispatches, so the ledger never claims a wait that is over.
_REARMED = "IN_PROGRESS"
_REASON = "operator"


@dataclass
class ClearedRun:
    """One parked run the operator's declaration re-armed (or would re-arm)."""

    run_id: str
    repo: str
    reset_at: float | None
    stories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "reset_at": self.reset_at,
            "stories": list(self.stories),
        }


@dataclass
class UnpauseResult:
    pause: QueuePause | None
    runs: list[ClearedRun]
    dry_run: bool

    @property
    def nothing_to_clear(self) -> bool:
        return self.pause is None and not self.runs


def _reset_epoch(config: dict) -> float | None:
    try:
        return float(config["rate_limit_reset_at"])
    except (KeyError, TypeError, ValueError):
        return None


def clear_rate_limit(
    store: QueueStore,
    registry: Registry,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> UnpauseResult:
    """Trust the operator: drop the pause and re-arm parked runs, no probe.

    The next dispatch's own rate-limit detection is the safety net — a window
    that is in fact still closed re-parks the run with a fresh reset, and the
    queue re-pauses exactly as in 32.2-001. Runs are found through the host
    registry (the read ``sdlc runs`` uses), so every repo is covered.

    Both the run status and the persisted reset epoch are cleared: the queue's
    discovery reads a ``RATE_LIMITED`` run with no epoch as a reset-less park
    and would reopen a five-hour window from it.
    """
    from sdlc.build import Ledger  # heavy module; only needed on this path

    moment = now or datetime.now(timezone.utc)
    pause = store.dispatch_pause()
    cleared: list[ClearedRun] = []
    for record in registry.records():
        if record.finished_at or pid_alive(record.pid):
            continue  # finished, or waiting in-process and will resume itself
        try:
            ledger = Ledger(Path(record.db))
            row = ledger.run_row(record.run_id)
            if row is None or row.get("status") != _RATE_LIMITED:
                continue
            config = ledger.run_config(record.run_id)
            stories = [
                s["story_id"]
                for s in ledger.story_rows(record.run_id)
                if s.get("status") == _RATE_LIMITED
            ]
        except Exception:  # noqa: BLE001 - one unreadable ledger must not block the rest
            continue
        run = ClearedRun(record.run_id, record.repo, _reset_epoch(config), stories)
        cleared.append(run)
        if dry_run:
            continue
        config.pop("rate_limit_reset_at", None)
        ledger.event_log(record.run_id, "", "info", "config", json.dumps(config))
        for story_id in stories:
            ledger.set_story_status(record.run_id, story_id, _REARMED)
        ledger.run_update_status(record.run_id, _REARMED)
        ledger.event_log(
            record.run_id, "", "info", "operator",
            f"operator declared the rate-limit window reset: cleared "
            f"rate_limit_reset_at ({run.reset_at}) and re-armed "
            f"{len(stories)} RATE_LIMITED story(ies) {stories}",
        )

    result = UnpauseResult(pause=pause, runs=cleared, dry_run=dry_run)
    if not dry_run and not result.nothing_to_clear:
        store.init()  # a runs-only clear on a host with no queue.db still audits
        store.clear_pause()
        store.record_pause_clear(
            paused_until=pause.paused_until if pause else None,
            runs_cleared=len(cleared),
            reason=_REASON,
            now=moment,
        )
    return result
