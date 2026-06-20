# ABOUTME: Read-side helpers for `sdlc state` — a greppable state-machine dump.
# ABOUTME: Story 10.1-001. Reads the ledger directly; never writes.

from __future__ import annotations

from sdlc.build import Ledger

__all__ = ["state_report", "format_state"]


def state_report(ledger: Ledger, run_id: str) -> list[dict]:
    """The persisted state-machine rows for ``run_id`` (one dict per stage row).

    A thin pass-through to :meth:`Ledger.state_rows` so the CLI and any future
    consumer share one shape: ``{story_id, stage_name, status, attempt, branch,
    pr_number}``.
    """
    return ledger.state_rows(run_id)


def format_state(rows: list[dict]) -> list[str]:
    """Render state rows as fixed-width, greppable lines (header first).

    The columns are stable so ``sdlc state | grep <story>`` and column-based
    tooling stay reliable. A missing branch falls back to the deterministic
    ``feature/<story_id>`` the build state machine uses; a missing PR renders
    as ``-``.
    """
    lines = [f"{'STORY':<16}{'STAGE':<11}{'STATUS':<13}{'ATT':<5}{'PR':<7}BRANCH"]
    for r in rows:
        pr = r.get("pr_number")
        pr_disp = f"#{pr}" if pr else "-"
        branch = r.get("branch") or f"feature/{r.get('story_id', '?')}"
        lines.append(
            f"{str(r.get('story_id', '?')):<16}"
            f"{str(r.get('stage_name', '?')):<11}"
            f"{str(r.get('status', '?')):<13}"
            f"{str(r.get('attempt', '?')):<5}"
            f"{pr_disp:<7}{branch}"
        )
    return lines
