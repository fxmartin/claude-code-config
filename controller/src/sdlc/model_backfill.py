# ABOUTME: Backfills the historical `stages.model` NULLs from the session logs and
# ABOUTME: scores per-attempt model coverage for `sdlc doctor` (Story 28.1-002).

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sdlc.build import _TERMINAL_RUN_STATES, Ledger
from sdlc.progress import dominant_model, model_of
from sdlc.usage_reconcile import logs_root_for, resolve_stage_log, select_runs

__all__ = [
    "BACKFILLED",
    "NOT_DISPATCHED",
    "RECORDED",
    "RECOVERABLE",
    "UNRECOVERABLE",
    "VERIFIED_STATUSES",
    "ModelAudit",
    "ModelBackfillResult",
    "backfill_models",
    "parse_log_model",
]

# Per-row outcomes.
#   RECORDED       the row already attributes a model (nothing to do)
#   BACKFILLED     the row was NULL and the log's model was written onto it
#   RECOVERABLE    the row is NULL and the log has a model, but this was a dry run
#   UNRECOVERABLE  the row is NULL and no model is recoverable — left NULL, counted
#   NOT_DISPATCHED a SKIPPED / still-running row: no agent ran, so no model is owed
RECORDED = "recorded"
BACKFILLED = "backfilled"
RECOVERABLE = "recoverable"
UNRECOVERABLE = "unrecoverable"
NOT_DISPATCHED = "not-dispatched"

# Reasons that mean the row carries a model attribution *now*. A dry-run
# RECOVERABLE row deliberately does not count: coverage reports what the ledger
# actually holds, not what a later pass could recover.
_POPULATED = frozenset({RECORDED, BACKFILLED})

# The statuses whose NULL model `sdlc doctor` can hold against the recording
# path (AC3). Necessary but not sufficient on its own: not every DONE row
# dispatched an agent (`reconcile._ensure_stages_done` synthesizes DONE rows for
# a parked-then-landed story), so doctor pairs this with a RECOVERABLE reason —
# a transcript that names a model the ledger failed to record.
VERIFIED_STATUSES = frozenset({"DONE"})


def parse_log_model(path: Path) -> str | None:
    """The model a stage attempt actually ran on, read from its session log.

    The terminal ``{"type":"result"}`` envelope's ``modelUsage`` map is the
    authoritative record — it names every model the session touched, so
    :func:`~sdlc.progress.dominant_model` picks the one that carried it. A
    session that crashed before emitting that envelope falls back to the most
    frequent ``message.model`` across its assistant turns, which is the only
    other place the served model is stated.

    Returns None for an unreadable file or a transcript with no model at all (a
    plain-text ``SDLC_AGENT_CMD`` run), so the caller leaves the row NULL and
    counts it rather than inventing an attribution.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    result_event: dict | None = None
    turn_models: Counter[str] = Counter()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue  # blank lines, stderr trailers, the KILLED marker
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result_event = event  # last one wins
            continue
        model = model_of(event)
        if model:
            turn_models[model] += 1

    if result_event is None:
        result_event = _whole_file_result(text)
    if result_event is not None:
        from_envelope = dominant_model(result_event.get("modelUsage"))
        if from_envelope:
            return from_envelope

    if not turn_models:
        return None
    # Ties broken by name so the recovered attribution is deterministic.
    return min(turn_models.items(), key=lambda item: (-item[1], item[0]))[0]


def _whole_file_result(text: str) -> dict | None:
    """Parse a transcript that is one (possibly pretty-printed) result envelope.

    The non-streaming dispatch path writes the whole ``--output-format json``
    envelope to the transcript, which line-by-line parsing misses when the JSON
    is indented across several lines (mirrors ``usage_reconcile``).
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        event = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(event, dict) and event.get("type") == "result":
        return event
    return None


@dataclass(frozen=True)
class ModelAudit:
    """One stage attempt's model attribution, and whether this pass wrote it."""

    run_id: str
    story_id: str
    stage_name: str
    attempt: int
    status: str
    reason: str
    model: str | None = None
    log_model: str | None = None
    updated: bool = False

    @property
    def label(self) -> str:
        return f"{self.story_id}/{self.stage_name}#{self.attempt}"

    @property
    def dispatched(self) -> bool:
        return self.reason != NOT_DISPATCHED

    @property
    def populated(self) -> bool:
        return self.reason in _POPULATED

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "story_id": self.story_id,
            "stage_name": self.stage_name,
            "attempt": self.attempt,
            "status": self.status,
            "reason": self.reason,
            "model": self.model,
            "log_model": self.log_model,
            "updated": self.updated,
        }


@dataclass
class ModelBackfillResult:
    """The outcome of a model backfill (or read-only coverage audit)."""

    run_ids: list[str] = field(default_factory=list)
    audits: list[ModelAudit] = field(default_factory=list)
    applied: bool = True

    @property
    def updated(self) -> list[ModelAudit]:
        return [a for a in self.audits if a.updated]

    @property
    def dispatched(self) -> int:
        """Attempts that actually ran an agent — the coverage denominator."""
        return sum(1 for a in self.audits if a.dispatched)

    @property
    def populated(self) -> int:
        return sum(1 for a in self.audits if a.populated)

    @property
    def unrecoverable(self) -> int:
        return sum(1 for a in self.audits if a.reason == UNRECOVERABLE)

    @property
    def coverage(self) -> float | None:
        """Share of dispatched attempts carrying a model, or None when there are none.

        None is an honest "nothing to score" (a run of only SKIPPED rows, or no
        runs at all) rather than a vacuous 100%.
        """
        if self.dispatched <= 0:
            return None
        return self.populated / self.dispatched

    @property
    def residual(self) -> list[ModelAudit]:
        """Every dispatched attempt still without a model, worst reason first."""
        order = {UNRECOVERABLE: 0, RECOVERABLE: 1}
        return sorted(
            (a for a in self.audits if a.dispatched and not a.populated),
            key=lambda a: (order.get(a.reason, 9), a.label),
        )

    def nulls_in(
        self, run_id: str, statuses: frozenset[str] | None = None
    ) -> list[ModelAudit]:
        """Dispatched attempts of ``run_id`` with no model, optionally by status.

        ``statuses`` narrows to the rows whose NULL is unambiguously a *recording*
        defect rather than an unknowable one — a DONE stage always produced a
        result envelope, so a NULL model there means the write regressed.
        """
        return [
            a
            for a in self.residual
            if a.run_id == run_id and (statuses is None or a.status in statuses)
        ]

    def counts(self) -> dict[str, int]:
        tally = {
            RECORDED: 0, BACKFILLED: 0, RECOVERABLE: 0,
            UNRECOVERABLE: 0, NOT_DISPATCHED: 0,
        }
        for audit in self.audits:
            tally[audit.reason] = tally.get(audit.reason, 0) + 1
        return tally

    def to_dict(self) -> dict:
        return {
            "run_ids": list(self.run_ids),
            "applied": self.applied,
            "dispatched": self.dispatched,
            "populated": self.populated,
            "unrecoverable": self.unrecoverable,
            "coverage": self.coverage,
            "counts": self.counts(),
            "updated": [a.to_dict() for a in self.updated],
            "residual": [a.to_dict() for a in self.residual],
        }


def _audit_row(
    ledger: Ledger, row: dict, logs_dir: Path, *, apply: bool, run_live: bool
) -> ModelAudit:
    """Classify one stage attempt's model attribution, backfilling a NULL one."""
    base = {
        "run_id": row["run_id"],
        "story_id": row["story_id"],
        "stage_name": row["stage_name"],
        "attempt": row["attempt"],
        "status": row["status"],
    }
    # A row left IN_PROGRESS under a *terminal* run is a crashed attempt, not a
    # running one — its log is final, so it is auditable like any other (the same
    # distinction usage reconciliation draws).
    not_dispatched = row["status"] == "SKIPPED" or (
        row["status"] == "IN_PROGRESS" and run_live
    )
    if not_dispatched:
        return ModelAudit(**base, reason=NOT_DISPATCHED, model=row.get("model"))

    recorded = row.get("model")
    if recorded:
        return ModelAudit(**base, reason=RECORDED, model=recorded)

    log_path = resolve_stage_log(
        logs_dir, row["story_id"], row["stage_name"], row["attempt"],
        output_path=row.get("output_path"),
    )
    log_model = parse_log_model(log_path) if log_path is not None else None
    if not log_model:
        # Left NULL on purpose (AC2): an unknown model is reported, never coerced.
        return ModelAudit(**base, reason=UNRECOVERABLE)
    if not apply:
        return ModelAudit(**base, reason=RECOVERABLE, log_model=log_model)

    ledger.stage_set_model(
        row["run_id"], row["story_id"], row["stage_name"], row["attempt"], log_model
    )
    return ModelAudit(
        **base, reason=BACKFILLED, model=log_model, log_model=log_model, updated=True
    )


def backfill_models(
    ledger: Ledger,
    run_id: str | None = None,
    *,
    all_runs: bool = False,
    run_limit: int | None = None,
    logs_root: Path | None = None,
    apply: bool = True,
) -> ModelBackfillResult:
    """Backfill NULL ``stages.model`` values from the session logs (Story 28.1-002).

    The ``model`` column exists since schema v11, but every row written before
    the verified per-attempt recording landed reads NULL — so cost-by-model, the
    whole point of the column, could only be re-derived by parsing logs. This
    pass does that derivation once and writes it *into* the ledger, from the same
    transcripts ``sdlc usage-reconcile`` reads.

    **Never fabricates.** A row whose transcript was pruned, or which only ever
    held plain-text output, is left NULL and counted ``unrecoverable`` — reported
    so the column's true coverage is known rather than silently coerced to a
    placeholder. A row that already attributes a model is never overwritten: the
    live recording (the agent's own result envelope) outranks any later re-read.

    **Idempotent.** Every write assigns an absolute value onto a NULL row, so a
    second pass finds it ``recorded`` and writes nothing.

    ``apply=False`` makes it a read-only audit — how ``sdlc doctor`` scores model
    coverage without mutating anything. Scope defaults to the latest run;
    ``all_runs`` sweeps every run and ``run_limit`` the most recent N.
    """
    logs_root = logs_root or logs_root_for(ledger.db_path)
    result = ModelBackfillResult(applied=apply)
    if not ledger.db_path.exists():
        return result

    result.run_ids = select_runs(ledger, run_id, all_runs, run_limit)
    for rid in result.run_ids:
        logs_dir = logs_root / rid
        run = ledger.run_row(rid) or {}
        run_live = run.get("status") not in _TERMINAL_RUN_STATES
        for row in ledger.stage_usage_rows(rid):
            result.audits.append(
                _audit_row(ledger, row, logs_dir, apply=apply, run_live=run_live)
            )
    return result
