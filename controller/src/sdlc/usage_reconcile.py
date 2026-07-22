# ABOUTME: Ledger-vs-logs usage reconciliation — backfill per-stage token/cost
# ABOUTME: usage from the session transcripts and score agreement (Story 28.1-001).

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sdlc.build import _TERMINAL_RUN_STATES, Ledger
from sdlc.progress import UsageAccumulator, usage_of

__all__ = [
    "AGREE",
    "COST_TOLERANCE_USD",
    "DEFAULT_TOLERANCE",
    "LOG_RECOVERED",
    "NO_LOG",
    "NO_USAGE",
    "SOURCE_LOG_RECOVERED",
    "SOURCE_LOG_RESULT",
    "STILL_DIVERGENT",
    "LogUsage",
    "StageAudit",
    "UsageReconcileResult",
    "logs_root_for",
    "parse_log_usage",
    "reconcile_usage",
    "resolve_stage_log",
]

# The ledger's usage columns, in the order the four token components are summed.
_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)

# Relative tolerance on the summed token total when deciding whether the ledger
# already agrees with the log. Non-zero because the two figures can legitimately
# differ by a rounding step (a live stream accrual vs the terminal result's own
# rollup), and a hair-trigger would report permanent, unfixable "drift".
DEFAULT_TOLERANCE = 0.01

# Absolute tolerance ($) on the recorded cost — half a cent, below any real
# per-stage spend, so a float round-trip through SQLite never reads as drift.
COST_TOLERANCE_USD = 0.005

# `stages.usage_source` provenance values written by this pass. NULL (neither of
# these) means the usage was measured live by dispatch, or never recorded.
SOURCE_LOG_RESULT = "log-result"
SOURCE_LOG_RECOVERED = "log-recovered"

# Per-row audit outcomes. AGREE is the only non-residual one; the other three are
# exactly the reasons `sdlc doctor` enumerates.
AGREE = "agree"
LOG_RECOVERED = "log-recovered"
STILL_DIVERGENT = "still-divergent"
NO_LOG = "no-log"
NO_USAGE = "no-usage"

# Reasons that mean "we could not check this row against ground truth" — they are
# reported, but excluded from the agreement rate so a repo whose logs were pruned
# never reports false agreement.
_UNVERIFIABLE = frozenset({NO_LOG, NO_USAGE})

# Ledger stage names whose transcript embeds the *originating* stage in its file
# name (`<story>-bugfix-<stage>-<seq>.log`) while the row itself only keys on the
# recovery role + sequence number. Matched with a glob on the middle segment.
_RECOVERY_STAGES = frozenset({"reask", "bugfix"})


def logs_root_for(db_path: Path) -> Path:
    """The transcript root for a ledger: ``<db>.logs`` (build.py's convention)."""
    return Path(f"{db_path}.logs")


# ---------------------------------------------------------------------------
# Reading a session log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogUsage:
    """Usage derived from one session transcript.

    ``complete`` is True only when the log carried a terminal ``{"type":"result"}``
    line with usage — the single authoritative cost record, since per-turn
    stream-json events carry tokens but no dollars. A crashed/interrupted session
    yields ``complete=False`` with ``cost_usd=None``: tokens are recoverable, cost
    is not, and it is never invented.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None
    session_id: str | None
    complete: bool

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, col) for col in _TOKEN_COLUMNS)


def _as_float(value: object) -> float | None:
    """``value`` as a float when it is a real number, else None (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _whole_file_event(text: str) -> dict | None:
    """Parse a transcript that is one (possibly pretty-printed) JSON envelope.

    The non-streaming dispatch path writes the entire ``--output-format json``
    envelope to the transcript, which line-by-line parsing misses when the JSON
    is indented across several lines. Returns the envelope only when it is a
    ``result`` object; anything else reads as None.
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


def parse_log_usage(path: Path) -> LogUsage | None:
    """Derive the ground-truth usage for one stage attempt from its session log.

    Session logs are raw ``stream-json``. The terminal ``result`` event is
    authoritative and wins whenever present. Without one (Issue #481: the session
    crashed, was killed, or the controller died mid-stage) the per-turn
    ``message.usage`` blocks are summed instead — tokens only.

    Returns None when the file is unreadable or carries no usage at all (a
    plain-text transcript from a custom ``SDLC_AGENT_CMD``), so the caller reports
    the row as unverifiable rather than comparing against a misleading zero.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    result_event: dict | None = None
    accumulator = UsageAccumulator()
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
        else:
            accumulator.observe(event)

    if result_event is None:
        result_event = _whole_file_event(text)

    from_result = usage_of(result_event) if result_event is not None else None
    cost = (
        _as_float(result_event.get("total_cost_usd"))
        if result_event is not None
        else None
    )
    session_id = accumulator.totals.session_id
    if result_event is not None and isinstance(result_event.get("session_id"), str):
        session_id = result_event["session_id"] or session_id

    if from_result:
        tokens = {col: int(from_result.get(col, 0)) for col in _TOKEN_COLUMNS}
        return LogUsage(**tokens, cost_usd=cost, session_id=session_id, complete=True)

    tokens = {col: getattr(accumulator.totals, col) for col in _TOKEN_COLUMNS}
    if not any(tokens.values()):
        return None
    # No authoritative result line: tokens are recovered, cost stays unavailable.
    return LogUsage(**tokens, cost_usd=None, session_id=session_id, complete=False)


def resolve_stage_log(
    logs_dir: Path,
    story_id: str,
    stage_name: str,
    attempt: int,
    output_path: str | None = None,
) -> Path | None:
    """Locate the transcript for one stage attempt, or None when it is gone.

    Resolution is driven by the *naming convention* build.py writes, derived from
    the row's own key — deliberately, not by the row's recorded ``output_path``.
    Overwrite-era rows (pre-PR #482) had their ``output_path`` rewritten to the
    cheap re-ask transcript when a contract-error recovery fired, so trusting it
    would copy the recovery's usage onto the original expensive session's row —
    the exact under-reporting this pass exists to undo.

    ``reask``/``bugfix`` rows key on ``(role, sequence)`` while their log embeds
    the originating stage, so those match by glob. ``output_path`` is consulted
    only as a fallback for a transcript outside the convention, and to
    disambiguate a glob that somehow matched more than one file.
    """
    if stage_name in _RECOVERY_STAGES:
        pattern = f"{story_id}-{stage_name}-*-{attempt}.log"
    else:
        pattern = f"{story_id}-{stage_name}-{attempt}.log"

    matches = sorted(logs_dir.glob(pattern)) if logs_dir.is_dir() else []
    if matches:
        if len(matches) > 1 and output_path:
            recorded = Path(output_path)
            if recorded in matches:
                return recorded
        return matches[0]

    if output_path:
        recorded = Path(output_path)
        if recorded.is_file():
            return recorded
    return None


# ---------------------------------------------------------------------------
# Comparing the ledger against the logs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageAudit:
    """One stage attempt's ledger-vs-log verdict, and whether it was rewritten."""

    run_id: str
    story_id: str
    stage_name: str
    attempt: int
    reason: str
    ledger_tokens: int | None = None
    log_tokens: int | None = None
    log_cost_usd: float | None = None
    updated: bool = False
    log_path: str | None = None

    @property
    def label(self) -> str:
        return f"{self.story_id}/{self.stage_name}#{self.attempt}"

    @property
    def unverifiable(self) -> bool:
        return self.reason in _UNVERIFIABLE

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "story_id": self.story_id,
            "stage_name": self.stage_name,
            "attempt": self.attempt,
            "reason": self.reason,
            "ledger_tokens": self.ledger_tokens,
            "log_tokens": self.log_tokens,
            "log_cost_usd": self.log_cost_usd,
            "updated": self.updated,
            "log_path": self.log_path,
        }


@dataclass
class UsageReconcileResult:
    """The outcome of a reconciliation (or dry-run audit) over one or more runs."""

    run_ids: list[str] = field(default_factory=list)
    audits: list[StageAudit] = field(default_factory=list)
    skipped_in_progress: int = 0
    applied: bool = True

    @property
    def updated(self) -> list[StageAudit]:
        return [a for a in self.audits if a.updated]

    @property
    def residual(self) -> list[StageAudit]:
        """Every attempt that does not agree with its log, worst reason first."""
        order = {STILL_DIVERGENT: 0, LOG_RECOVERED: 1, NO_LOG: 2, NO_USAGE: 3}
        return sorted(
            (a for a in self.audits if a.reason != AGREE),
            key=lambda a: (order.get(a.reason, 9), a.label),
        )

    @property
    def unverifiable(self) -> int:
        """Attempts with no readable usage on disk — excluded from the rate."""
        return sum(1 for a in self.audits if a.unverifiable)

    @property
    def verifiable(self) -> int:
        """Attempts whose log yielded usage to compare against."""
        return len(self.audits) - self.unverifiable

    @property
    def matched(self) -> int:
        return sum(1 for a in self.audits if a.reason == AGREE)

    @property
    def agreement_rate(self) -> float | None:
        """Share of *verifiable* attempts whose ledger usage matches the log.

        None when nothing is verifiable (no runs, or every transcript pruned) —
        an honest "unknown" rather than a vacuous 100%.
        """
        if self.verifiable <= 0:
            return None
        return self.matched / self.verifiable

    def counts(self) -> dict[str, int]:
        tally = {AGREE: 0, LOG_RECOVERED: 0, STILL_DIVERGENT: 0, NO_LOG: 0, NO_USAGE: 0}
        for audit in self.audits:
            tally[audit.reason] = tally.get(audit.reason, 0) + 1
        return tally

    def to_dict(self) -> dict:
        return {
            "run_ids": list(self.run_ids),
            "applied": self.applied,
            "skipped_in_progress": self.skipped_in_progress,
            "verifiable": self.verifiable,
            "matched": self.matched,
            "unverifiable": self.unverifiable,
            "agreement_rate": self.agreement_rate,
            "counts": self.counts(),
            "updated": [a.to_dict() for a in self.updated],
            "residual": [a.to_dict() for a in self.residual],
        }


def _row_tokens(row: dict) -> int | None:
    """The row's summed token total, or None when it recorded no usage at all."""
    values = [row.get(col) for col in _TOKEN_COLUMNS]
    if all(v is None for v in values):
        return None
    return sum(int(v or 0) for v in values)


def _tokens_match(ledger_tokens: int | None, log_tokens: int, tolerance: float) -> bool:
    if ledger_tokens is None:
        return False
    allowed = max(1.0, tolerance * max(abs(ledger_tokens), abs(log_tokens)))
    return abs(ledger_tokens - log_tokens) <= allowed


def _cost_matches(row_cost: float | None, log_cost: float, tolerance: float) -> bool:
    if row_cost is None:
        return False
    allowed = max(COST_TOLERANCE_USD, tolerance * abs(log_cost))
    return abs(row_cost - log_cost) <= allowed


def _write_usage(
    ledger: Ledger, row: dict, log: LogUsage, *, source: str, cost_usd: float | None
) -> None:
    ledger.stage_set_usage(
        row["run_id"], row["story_id"], row["stage_name"], row["attempt"],
        session_id=log.session_id or row.get("session_id"),
        input_tokens=log.input_tokens,
        output_tokens=log.output_tokens,
        cache_read_tokens=log.cache_read_tokens,
        cache_creation_tokens=log.cache_creation_tokens,
        cost_usd=cost_usd,
        usage_source=source,
    )


def _audit_row(
    ledger: Ledger,
    row: dict,
    logs_dir: Path,
    *,
    tolerance: float,
    apply: bool,
) -> StageAudit:
    """Compare one stage attempt against its log, backfilling it when they differ."""
    ledger_tokens = _row_tokens(row)
    base = {
        "run_id": row["run_id"],
        "story_id": row["story_id"],
        "stage_name": row["stage_name"],
        "attempt": row["attempt"],
        "ledger_tokens": ledger_tokens,
    }

    log_path = resolve_stage_log(
        logs_dir, row["story_id"], row["stage_name"], row["attempt"],
        output_path=row.get("output_path"),
    )
    if log_path is None:
        return StageAudit(**base, reason=NO_LOG)

    log = parse_log_usage(log_path)
    if log is None:
        return StageAudit(**base, reason=NO_USAGE, log_path=str(log_path))

    base["log_tokens"] = log.total_tokens
    base["log_cost_usd"] = log.cost_usd
    base["log_path"] = str(log_path)

    if not log.complete:
        # Issue #481: crashed session. Tokens are recoverable by summing the
        # streamed turns; cost is not, so any cost already on the row is left
        # exactly as-is and none is invented. The `log-recovered` stamp is what
        # makes the provenance auditable — and what makes a re-run a no-op, since
        # writes assign the log's absolute totals rather than accumulating.
        needs_write = not _tokens_match(ledger_tokens, log.total_tokens, tolerance) or (
            row.get("usage_source") != SOURCE_LOG_RECOVERED
        )
        if apply and needs_write:
            _write_usage(
                ledger, row, log,
                source=SOURCE_LOG_RECOVERED, cost_usd=row.get("cost_usd"),
            )
            return StageAudit(**base, reason=LOG_RECOVERED, updated=True)
        return StageAudit(**base, reason=LOG_RECOVERED)

    agrees = _tokens_match(ledger_tokens, log.total_tokens, tolerance) and (
        log.cost_usd is None or _cost_matches(row.get("cost_usd"), log.cost_usd, tolerance)
    )
    if agrees:
        return StageAudit(**base, reason=AGREE)
    if not apply:
        return StageAudit(**base, reason=STILL_DIVERGENT)
    _write_usage(ledger, row, log, source=SOURCE_LOG_RESULT, cost_usd=log.cost_usd)
    return StageAudit(**base, reason=AGREE, updated=True)


def _select_runs(
    ledger: Ledger,
    run_id: str | None,
    all_runs: bool,
    run_limit: int | None,
) -> list[str]:
    """The run ids to sweep: an explicit one, every run, or the most recent N."""
    if run_id is not None:
        return [run_id]
    if all_runs:
        return [run["id"] for run in ledger.list_runs(limit=10_000)]
    if run_limit is not None:
        return [run["id"] for run in ledger.list_runs(limit=run_limit)]
    latest = ledger.latest_run_id()
    return [latest] if latest else []


def reconcile_usage(
    ledger: Ledger,
    run_id: str | None = None,
    *,
    all_runs: bool = False,
    run_limit: int | None = None,
    logs_root: Path | None = None,
    apply: bool = True,
    tolerance: float = DEFAULT_TOLERANCE,
) -> UsageReconcileResult:
    """Reconcile the ledger's per-stage usage against the session logs.

    For each terminal stage attempt it locates the attempt's transcript, derives
    the ground-truth usage from it, and — where the ledger is missing usage the
    log carries or the two disagree — writes the log-derived figures onto that
    exact attempt row via :meth:`Ledger.stage_set_usage`.

    **Idempotent by construction.** Every write *assigns* the log's absolute
    totals; nothing is ever accumulated, so a second pass finds the row already
    matching and writes nothing. A row still ``IN_PROGRESS`` under a **live** run
    is skipped entirely (its log is not final, and the controller may still write
    the authoritative usage itself) and counted in ``skipped_in_progress``; the
    same row under a *terminal* run is the crashed attempt itself and is
    reconciled — that is the Issue #481 case.

    **Never fabricates.** A crashed session (no terminal ``result`` line) yields
    token counts only, stamped ``log-recovered``, with cost left untouched — the
    per-turn stream-json events carry no dollars, so there is nothing honest to
    write there. A row whose transcript was pruned is reported ``no-log`` and left
    completely alone.

    ``apply=False`` makes the pass a read-only audit, which is how ``sdlc doctor``
    scores agreement without mutating anything. Scope defaults to the latest run;
    ``all_runs`` sweeps every run and ``run_limit`` the most recent N.
    """
    logs_root = logs_root or logs_root_for(ledger.db_path)
    result = UsageReconcileResult(applied=apply)
    if not ledger.db_path.exists():
        return result

    result.run_ids = _select_runs(ledger, run_id, all_runs, run_limit)
    for rid in result.run_ids:
        logs_dir = logs_root / rid
        # An IN_PROGRESS stage row under a *terminal* run is not running — it is a
        # crashed attempt the controller never got to finish (build.py leaves the
        # interrupted row exactly so). That is the Issue #481 shape this pass has
        # to recover, so only a row under a still-live run is skipped: there, a
        # controller may still write the authoritative usage itself.
        run = ledger.run_row(rid) or {}
        run_live = run.get("status") not in _TERMINAL_RUN_STATES
        for row in ledger.stage_usage_rows(rid):
            if row["status"] == "IN_PROGRESS" and run_live:
                result.skipped_in_progress += 1
                continue
            result.audits.append(
                _audit_row(ledger, row, logs_dir, tolerance=tolerance, apply=apply)
            )
    return result
