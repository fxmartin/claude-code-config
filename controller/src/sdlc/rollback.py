# ABOUTME: Controller-native rollback to a prior ledger checkpoint (Story 10.2-001).
# ABOUTME: Resets the stories after a checkpoint to TODO; refuses to discard merged work.

from __future__ import annotations

from dataclasses import dataclass, field

from sdlc.build import Ledger

__all__ = [
    "RollbackError",
    "RollbackResult",
    "list_checkpoints",
    "run_rollback",
]


class RollbackError(Exception):
    """Rollback was refused — the checkpoint is unknown, there is no run, or the
    rollback would discard a story whose PR has already merged."""


@dataclass
class RollbackResult:
    """The outcome of a rollback invocation."""

    run_id: str
    checkpoint: str
    reset_stories: list[str] = field(default_factory=list)
    kept_stories: list[str] = field(default_factory=list)


def _story_merged(attempts: list[dict]) -> bool:
    """Whether a story's merge stage has a DONE attempt — i.e. its PR merged.

    The ``merge`` stage transitioning to DONE is the on-disk signal that the
    agent merged the PR; that is committed work rollback must not silently lose.
    """
    return any(a.get("name") == "merge" and a.get("status") == "DONE" for a in attempts)


def list_checkpoints(ledger: Ledger, run_id: str) -> list[dict]:
    """The run's stories in execution order, each a candidate checkpoint.

    Returns ``[{story_id, status, pr_number, merged}]`` in the order the stories
    were scheduled (the ledger's insertion order, which mirrors cohort order).
    ``merged`` flags stories whose PR has already merged — rolling *past* such a
    story is refused by :func:`run_rollback`.
    """
    breakdown = ledger.stage_breakdown(run_id)
    out: list[dict] = []
    for row in ledger.story_rows(run_id):
        sid = row["story_id"]
        out.append(
            {
                "story_id": sid,
                "status": row.get("status"),
                "pr_number": row.get("pr_number"),
                "merged": _story_merged(breakdown.get(sid, [])),
            }
        )
    return out


def run_rollback(
    ledger: Ledger, run_id: str | None, checkpoint: str
) -> RollbackResult:
    """Roll ``run_id`` back to ``checkpoint`` (a story id), then reopen the run.

    The checkpoint story and every story scheduled before it are kept exactly as
    they are; every story scheduled *after* it is reset to a fresh unbuilt state
    (stage rows deleted, PR/branch cleared, status TODO) so the next ``resume``/
    ``build`` rebuilds only those. The run is reopened to ``IN_PROGRESS``.

    Guard rails (each raises :class:`RollbackError`, leaving the ledger
    untouched):

    - there is no run to roll back, or
    - ``checkpoint`` is not a story in the run, or
    - resetting would discard a story whose PR has already merged.

    Rolling back to the last story is a benign no-op (nothing scheduled after it).
    """
    rid = run_id or ledger.latest_run_id()
    if rid is None:
        raise RollbackError("no run found in the ledger — nothing to roll back.")

    checkpoints = list_checkpoints(ledger, rid)
    order = [c["story_id"] for c in checkpoints]
    if checkpoint not in order:
        known = ", ".join(order) if order else "(none)"
        raise RollbackError(
            f"checkpoint '{checkpoint}' does not exist in run {rid[:8]} — "
            f"known checkpoints: {known}."
        )

    idx = order.index(checkpoint)
    kept = order[: idx + 1]
    reset = order[idx + 1 :]

    # Refuse before mutating anything if any to-be-reset story is merged.
    merged_flag = {c["story_id"]: c["merged"] for c in checkpoints}
    would_discard = [sid for sid in reset if merged_flag.get(sid)]
    if would_discard:
        raise RollbackError(
            f"refusing to roll back run {rid[:8]} to '{checkpoint}': it would "
            f"discard already-merged work ({', '.join(would_discard)}). A merged "
            "PR cannot be unwound by the ledger — revert it in git instead."
        )

    if not reset:
        # Checkpoint is the latest story; there is nothing after it to undo.
        return RollbackResult(run_id=rid, checkpoint=checkpoint, kept_stories=kept)

    for sid in reset:
        ledger.reset_story(rid, sid)
    ledger.run_update_status(rid, "IN_PROGRESS")
    ledger.event_log(
        rid,
        "",
        "warn",
        "controller",
        f"rollback to '{checkpoint}': reset {len(reset)} story(ies) "
        f"({', '.join(reset)}) to TODO",
    )

    return RollbackResult(
        run_id=rid,
        checkpoint=checkpoint,
        reset_stories=reset,
        kept_stories=kept,
    )
