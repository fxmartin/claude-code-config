# ABOUTME: Reproducible agentic eval harness (Story 18.1-001) — drives an agent
# ABOUTME: headlessly over a fixed ticket set on a sample repo and scores the diff.

from __future__ import annotations

import functools
import platform
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from sdlc.capability import ProbeRunner, ProbeStatus, probe_harness
from sdlc.contracts import AGENT_SCHEMAS, ContractError, _result_wrapper
from sdlc.cost_estimate import DEFAULT_USD_PER_MILLION_TOKENS, notional_cost
from sdlc.dispatch import AgentResult, RateLimitError, dispatch_agent
from sdlc.harness import (
    DEFAULT_HARNESS,
    HarnessConfig,
    HarnessError,
    dispatch_on_harness,
    resolve_harness,
)
from sdlc.model_routing import BALANCED, select_model
from sdlc.rate_limit import seconds_until_reset, within_wait_cap
from sdlc.usage import (
    APPROXIMATE_SOURCES,
    ESTIMATED,
    MEASURED,
    MIXED,
    UNAVAILABLE,
    UNAVAILABLE_USAGE,
    USAGE_COMPONENTS,
    TokenBreakdown,
    aggregate_source,
    breakdown_from_envelope,
    harness_breakdown,
    usage_is_tracked,
)

# Story 31.2-003: cost provenance reuses the token vocabulary above for the
# concepts that carry over (a harness's own reported dollar figure is
# ``MEASURED``, a $/Mtok-derived guess is ``ESTIMATED``, no figure at all is
# ``UNAVAILABLE``, and a ticket's runs disagreeing is ``MIXED``) and adds two
# the token axis never needs: a harness with no per-token price to derive at
# all (``NOT_METERED`` — the field case: oMLX's own ``cost: 0`` telemetry is
# not a saving, it is the absence of a meter) and a non-metered harness with
# an explicit, recorded rate instead of the hosted $/Mtok assumption
# (``LOCAL_RATE``).
NOT_METERED = "not_metered"
LOCAL_RATE = "local_rate"

# Cost sources that are a derived/assumed figure rather than a harness's own
# billed number — rendered "~"-prefixed, mirroring APPROXIMATE_SOURCES for
# tokens.
APPROXIMATE_COST_SOURCES: frozenset[str] = frozenset({ESTIMATED, LOCAL_RATE, MIXED})

# Default per-story headless dispatch ceiling (seconds). An eval ticket is a small
# edit on a tiny repo, so it should finish well inside a build's full timeout.
DEFAULT_TICKET_TIMEOUT_S = 600

# In-process rate-limit auto-wait cap (Story 31.2-001), mirroring build.py's
# ``rate_limit_max_wait_s`` (~one Max rolling window). A wait beyond this is
# treated as a lost run rather than held indefinitely inside an eval sweep.
DEFAULT_RATE_LIMIT_MAX_WAIT_S = 18000

# Label used for the aggregate row in a scoreboard.
OVERALL_LABEL = "OVERALL"


class EvalConfigError(Exception):
    """A malformed or incomplete eval config (missing fields, bad types)."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """One eval ticket: a prompt the agent works, plus an optional quality check.

    ``quality_cmd`` is run in the post-dispatch working copy; exit 0 = pass. When
    omitted the run carries no quality signal (``quality_pass`` stays ``None``).
    """

    id: str
    prompt: str
    quality_cmd: list[str] | None = None


@dataclass(frozen=True)
class EvalConfig:
    """A versioned eval definition: a sample target, a ticket set, and ``n`` runs.

    ``target`` is a directory of plain files (NOT a nested git repo) copied into a
    throwaway workspace and ``git init``-ed per run, so the eval never mutates the
    framework repo. ``seed`` is recorded for reproducibility provenance; model
    sampling itself stays non-deterministic, so re-runs match only within variance.
    """

    name: str
    target: Path
    tickets: list[Ticket]
    n: int = 1
    seed: int | None = None
    agent_type: str = "build"
    usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS
    model: str | None = None
    harness: str | None = None

    def __post_init__(self) -> None:
        # Issue #435: pin a concrete model so an eval never silently runs on the
        # user's current CLI default. When the config names none, resolve the
        # Balanced-profile model for the eval's agent role (e.g. build → sonnet).
        if self.model is None:
            object.__setattr__(self, "model", select_model(self.agent_type, BALANCED))
        # Story 31.1-001: pin a concrete harness name the same way — so a
        # scoreboard never records "whatever the CLI happened to default to".
        # A config naming none resolves to the built-in claude default, which
        # dispatches byte-identically to today (AC3).
        if self.harness is None:
            object.__setattr__(self, "harness", DEFAULT_HARNESS)


@dataclass(frozen=True)
class DiffStats:
    """Line/file deltas parsed from ``git diff --numstat`` of a scored run."""

    added: int = 0
    removed: int = 0
    files: int = 0

    @property
    def net(self) -> int:
        return self.added - self.removed


@dataclass(frozen=True)
class RunResult:
    """The scored outcome of a single ticket × run-index dispatch."""

    ticket_id: str
    run_index: int
    diff: DiffStats
    wall_s: float
    tokens: int | None = None
    cost_usd: float | None = None
    quality_pass: bool | None = None
    error: str | None = None
    # Issue #435: "ok" for a clean run, "contract_miss" when the agent produced a
    # real diff/tokens but failed the result-block contract, "error" for an
    # infrastructure failure that discarded the run. Distinguishes a recoverable,
    # still-scored miss from a lost run in the scoreboard's provenance.
    status: str = "ok"
    # Story 31.2-001: seconds this run spent waiting in-process on a rate limit,
    # already excluded from ``wall_s`` above (agent time, not agent-time-plus-
    # quota-backoff). ``None`` (not 0) when the run never stalled.
    stall_s: float | None = None
    # Story 31.2-002: the four token components this run's ``tokens`` total is
    # made of, plus their provenance. Carried individually because a total is a
    # lossy summary — 15,160 cache-write tokens and 14,152 cache-read tokens are
    # not parity — and the comparator cannot judge a mix it was never given.
    usage: TokenBreakdown = UNAVAILABLE_USAGE
    # Story 31.2-003: this run's cost provenance — see the vocabulary above.
    # Distinguishes a harness with no meter at all (``NOT_METERED``, ``cost_usd``
    # stays ``None``) from a run that simply produced no tokens to derive a
    # figure from (``UNAVAILABLE``) — collapsing the two would read a local
    # harness's "no meter" as the same blank a genuinely lost run gets.
    cost_source: str = UNAVAILABLE


@dataclass(frozen=True)
class TicketScore:
    """Per-ticket aggregate over its ``runs`` runs (means; ``None`` when absent)."""

    ticket_id: str
    runs: int
    errors: int
    loc_added_mean: float
    loc_removed_mean: float
    loc_net_mean: float
    tokens_mean: float | None
    cost_mean: float | None
    wall_mean: float
    quality_pass_rate: float | None
    # Story 31.2-001: mean rate-limit wait already excluded from ``wall_mean``
    # (the None-not-zero convention: absent when no run in this ticket stalled).
    stall_mean: float | None = None
    # Story 31.2-002: ``tokens_mean``'s component breakdown, each mean carried to
    # the scoreboard rather than collapsed at capture, plus the provenance of the
    # figure (measured / estimated / external / mixed / unavailable). Same
    # None-not-zero convention throughout.
    input_tokens_mean: float | None = None
    output_tokens_mean: float | None = None
    cache_read_tokens_mean: float | None = None
    cache_creation_tokens_mean: float | None = None
    tokens_source: str = UNAVAILABLE
    # Story 31.2-003: ``cost_mean``'s provenance, folded across this ticket's
    # runs the same way ``tokens_source`` is — "the scoreboard says which" is
    # the AC this satisfies: a not-metered harness reads plainly, never as a
    # silent zero.
    cost_source: str = UNAVAILABLE


@dataclass(frozen=True)
class Provenance:
    """The conditions that produced a scoreboard — Story 31.1-002 AC1.

    ``config_name`` + ``seed`` + ``ticket_ids`` together are the *ticket-set
    identity* ``eval-compare`` checks before it treats two scoreboards as a
    valid A/B: comparing runs built from different work is not a comparison.
    ``harness_version`` is the registry's declared ``probe`` command output —
    ``None`` when the harness declares no probe (e.g. the built-in claude
    harness), which is an absent fact, not a failed one. ``host`` is coarse
    (hostname/arch) by design, never a hardware fingerprint. ``timestamp`` is
    UTC ISO-8601 (``%Y-%m-%dT%H:%M:%SZ``).
    """

    harness: str
    model: str | None
    harness_version: str | None
    host: str
    config_name: str
    seed: int | None
    ticket_ids: list[str]
    n: int
    timestamp: str
    # Story 31.2-003: whether this run's harness has a real/notional $/Mtok
    # price at all (``True``, the default — every existing scoreboard/caller
    # keeps today's hosted assumption), and the explicit local rate configured
    # for it, if any. Recording both here is what makes the assumption travel
    # with the number (AC5) rather than living only in a config file nobody
    # re-reads later.
    cost_metered: bool = True
    local_rate_usd_per_million_tokens: float | None = None


def host_identifier() -> str:
    """A coarse host id (``hostname/machine-arch``) — never a fingerprint."""
    return f"{socket.gethostname()}/{platform.machine()}"


def utc_timestamp() -> str:
    """The current UTC time as ``%Y-%m-%dT%H:%M:%SZ`` (second precision)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_provenance(
    config: EvalConfig,
    *,
    harness_version: str | None = None,
    host: str | None = None,
    timestamp: str | None = None,
    metered: bool = True,
    local_rate_usd_per_million_tokens: float | None = None,
) -> Provenance:
    """Assemble a scoreboard's provenance block from a resolved :class:`EvalConfig`.

    ``config.harness``/``config.model`` are always concrete by this point
    (``EvalConfig.__post_init__`` pins both), so the block never records a
    guessed default. ``harness_version`` is the caller's job to supply (it
    requires running the harness's declared probe, an I/O step this pure
    assembly function does not perform itself). ``metered``/
    ``local_rate_usd_per_million_tokens`` (Story 31.2-003) are the resolved
    harness's own cost-provenance fields — the caller's job too, since
    resolving a :class:`~sdlc.harness.HarnessConfig` is I/O this assembly
    function does not perform. Both default to today's hosted assumption, so
    a caller that never passes them keeps unchanged behaviour (AC1).
    """
    return Provenance(
        harness=config.harness or DEFAULT_HARNESS,
        model=config.model,
        harness_version=harness_version,
        host=host if host is not None else host_identifier(),
        config_name=config.name,
        seed=config.seed,
        ticket_ids=[t.id for t in config.tickets],
        n=config.n,
        timestamp=timestamp if timestamp is not None else utc_timestamp(),
        cost_metered=metered,
        local_rate_usd_per_million_tokens=local_rate_usd_per_million_tokens,
    )


@dataclass(frozen=True)
class Scoreboard:
    """The full eval result: one :class:`TicketScore` per ticket plus an overall."""

    config_name: str
    tickets: list[TicketScore] = field(default_factory=list)
    overall: TicketScore | None = None
    # Story 31.1-001 AC1: the harness every ticket dispatch actually ran on, so
    # per-harness scoreboards are comparable by construction, never guessed from
    # context.
    harness: str = DEFAULT_HARNESS
    # Story 31.1-002 AC1: full run provenance (harness/model/version/host/
    # ticket-set identity/n/timestamp). ``None`` for a caller that never builds
    # one — legacy scoreboards and any direct `aggregate()` call that skips it.
    provenance: Provenance | None = None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(path: Path) -> EvalConfig:
    """Parse and validate a YAML eval config into an :class:`EvalConfig`.

    The ``target`` path and quality-command paths are resolved relative to the
    config file's own directory, so a config + sample target + ticket set form a
    self-contained, versioned bundle. Raises :class:`EvalConfigError` on any
    missing required field or wrong type rather than failing deep in the runner.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalConfigError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise EvalConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise EvalConfigError(f"config must be a mapping, got {type(raw).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise EvalConfigError("config 'name' is required and must be a non-empty string")

    target_rel = raw.get("target")
    if not isinstance(target_rel, str) or not target_rel:
        raise EvalConfigError("config 'target' is required and must be a path string")
    target = (path.parent / target_rel).resolve()

    n = raw.get("n", 1)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise EvalConfigError("config 'n' must be an integer >= 1")

    seed = raw.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise EvalConfigError("config 'seed' must be an integer when set")

    agent_type = raw.get("agent_type", "build")
    if not isinstance(agent_type, str) or not agent_type:
        raise EvalConfigError("config 'agent_type' must be a non-empty string")

    # Issue #435: an optional explicit model pin. When absent, EvalConfig resolves
    # the Balanced-profile model for agent_type so the eval is never run on the
    # user's silent CLI default.
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise EvalConfigError("config 'model' must be a non-empty string when set")

    # Story 31.1-001: an optional harness name, resolved through the registry at
    # dispatch time (never here — loading a config does no I/O beyond itself).
    # Absent means the built-in claude default (EvalConfig pins it below).
    harness = raw.get("harness")
    if harness is not None and (not isinstance(harness, str) or not harness):
        raise EvalConfigError("config 'harness' must be a non-empty string when set")

    raw_tickets = raw.get("tickets")
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise EvalConfigError("config 'tickets' is required and must be a non-empty list")

    tickets = [_parse_ticket(item, index=i) for i, item in enumerate(raw_tickets)]
    seen: set[str] = set()
    for ticket in tickets:
        if ticket.id in seen:
            raise EvalConfigError(f"duplicate ticket id: {ticket.id!r}")
        seen.add(ticket.id)

    return EvalConfig(
        name=name,
        target=target,
        tickets=tickets,
        n=n,
        seed=seed,
        agent_type=agent_type,
        model=model,
        harness=harness,
    )


def _parse_ticket(item: Any, *, index: int) -> Ticket:
    if not isinstance(item, dict):
        raise EvalConfigError(f"ticket #{index} must be a mapping")
    ticket_id = item.get("id")
    if not isinstance(ticket_id, str) or not ticket_id:
        raise EvalConfigError(f"ticket #{index} 'id' is required and must be a string")
    prompt = item.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise EvalConfigError(f"ticket {ticket_id!r} 'prompt' is required and must be a string")
    quality_cmd = item.get("quality_cmd")
    if quality_cmd is not None:
        if not isinstance(quality_cmd, list) or not all(
            isinstance(part, str) for part in quality_cmd
        ):
            raise EvalConfigError(
                f"ticket {ticket_id!r} 'quality_cmd' must be a list of strings"
            )
    return Ticket(id=ticket_id, prompt=prompt, quality_cmd=quality_cmd)


# ---------------------------------------------------------------------------
# Scoring primitives (pure — the unit-tested core)
# ---------------------------------------------------------------------------


def parse_diff_numstat(numstat: str) -> DiffStats:
    """Parse ``git diff --numstat`` output into added/removed/file counts.

    Each line is ``<added>\\t<removed>\\t<path>``; a binary file reports ``-`` for
    both counts (counted as a touched file, zero lines). Blank lines are ignored.
    """
    added = removed = files = 0
    for line in numstat.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        a, r = parts[0], parts[1]
        if a != "-":
            added += int(a)
        if r != "-":
            removed += int(r)
    return DiffStats(added=added, removed=removed, files=files)


def tokens_from_usage(usage: dict[str, Any] | None) -> int | None:
    """Sum the four token components of an agent usage envelope, or ``None``.

    ``None`` (not 0) means the agent carried no usage (a plain-text custom agent),
    so an absent figure is never confused with a genuine zero.
    """
    return breakdown_from_envelope(usage).total


def _cost_from(
    cost_usd: float | None,
    usage: TokenBreakdown,
    *,
    usd_per_million_tokens: float,
    metered: bool = True,
    local_rate_usd_per_million_tokens: float | None = None,
) -> tuple[float | None, str]:
    """Notional cost from an explicit envelope cost or, failing that, token usage.

    Returns ``(cost_usd, cost_source)`` — see the provenance vocabulary above.
    The envelope ``cost_usd`` wins when present *and the harness is metered*;
    otherwise a notional figure is derived from total tokens (the controller's
    notional-$ convention). ``None`` when neither a cost nor any token usage is
    available. Shared by :func:`result_cost` and the contract-miss path (issue
    #435), which only has a raw usage envelope and cost, not an
    :class:`AgentResult`.

    Story 31.2-002: a harness whose usage is *unavailable* has no derived cost
    either — the cost axis is only as honest as the token axis it comes from.
    Callers gate ``cost_usd`` on the same capability, since ``usage_tracking``
    covers usage *and* cost.

    Story 31.2-003: ``metered=False`` (a harness with no per-token price, e.g.
    local inference) ignores ``cost_usd`` entirely — the field case: a
    literal ``cost: 0`` from a local harness's own telemetry is not a real
    zero-dollar spend, so it must never be trusted at face value. Such a
    harness's cost is either an explicit ``local_rate_usd_per_million_tokens``
    (a recorded assumption, derived from tokens the same way the hosted
    convention is) or ``None`` labelled ``NOT_METERED`` — never a number, so a
    comparator can never read it as a saving.
    """
    if not metered:
        if local_rate_usd_per_million_tokens is None:
            return None, NOT_METERED
        tokens = usage.total
        if tokens is None:
            return None, UNAVAILABLE
        return (
            notional_cost(tokens, usd_per_million_tokens=local_rate_usd_per_million_tokens),
            LOCAL_RATE,
        )
    if cost_usd is not None:
        return float(cost_usd), MEASURED
    tokens = usage.total
    if tokens is None:
        return None, UNAVAILABLE
    return notional_cost(tokens, usd_per_million_tokens=usd_per_million_tokens), ESTIMATED


def result_cost(
    result: AgentResult,
    *,
    usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS,
    metered: bool = True,
    local_rate_usd_per_million_tokens: float | None = None,
) -> float | None:
    """Notional cost of a run: the envelope ``cost_usd`` if present, else derived.

    Falls back to a notional figure computed from total tokens (mirrors the
    controller's notional-$ convention) so a run still carries a comparable cost
    even when the agent envelope omits ``total_cost_usd``. ``None`` when neither a
    cost nor any token usage is available, or when the harness is not metered
    (Story 31.2-003) and carries no configured local rate.
    """
    cost, _source = _cost_from(
        result.cost_usd,
        breakdown_from_envelope(result.usage),
        usd_per_million_tokens=usd_per_million_tokens,
        metered=metered,
        local_rate_usd_per_million_tokens=local_rate_usd_per_million_tokens,
    )
    return cost


def run_quality_check(cmd: Sequence[str] | None, cwd: Path) -> bool | None:
    """Run a ticket's quality command in ``cwd``; ``True`` on exit 0, else ``False``.

    ``None`` when no command is configured (the run carries no quality signal). A
    command that fails to launch (missing binary) scores ``False`` rather than
    raising, so one broken check never aborts the whole eval.
    """
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _optional_mean(values: Sequence[float | int | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _fold_cost_sources(sources: Sequence[str]) -> str:
    """Fold several runs' cost provenance into one aggregate label.

    Mirrors ``usage.aggregate_source`` for the cost axis: an unavailable run
    contributes no figure and is ignored; runs that agree keep their shared
    source; runs that disagree are ``MIXED`` — an aggregate of (say) a metered
    run and a not-metered run is neither, and must not be compared as one.
    """
    distinct = {s for s in sources if s != UNAVAILABLE}
    if not distinct:
        return UNAVAILABLE
    if len(distinct) == 1:
        return next(iter(distinct))
    return MIXED


def _score_runs(ticket_id: str, runs: Sequence[RunResult]) -> TicketScore:
    quality = [r.quality_pass for r in runs if r.quality_pass is not None]
    usages = [r.usage for r in runs]
    components = {
        name: _optional_mean([u.components[name] for u in usages])
        for name in USAGE_COMPONENTS
    }
    return TicketScore(
        ticket_id=ticket_id,
        runs=len(runs),
        errors=sum(1 for r in runs if r.error is not None),
        loc_added_mean=_mean([r.diff.added for r in runs]),
        loc_removed_mean=_mean([r.diff.removed for r in runs]),
        loc_net_mean=_mean([r.diff.net for r in runs]),
        tokens_mean=_optional_mean([r.tokens for r in runs]),
        cost_mean=_optional_mean([r.cost_usd for r in runs]),
        wall_mean=_mean([r.wall_s for r in runs]),
        quality_pass_rate=(
            sum(1 for q in quality if q) / len(quality) if quality else None
        ),
        stall_mean=_optional_mean([r.stall_s for r in runs]),
        input_tokens_mean=components["input"],
        output_tokens_mean=components["output"],
        cache_read_tokens_mean=components["cache_read"],
        cache_creation_tokens_mean=components["cache_creation"],
        tokens_source=aggregate_source(usages),
        cost_source=_fold_cost_sources([r.cost_source for r in runs]),
    )


def aggregate(
    results: Sequence[RunResult],
    config_name: str,
    *,
    harness: str | None = None,
    provenance: Provenance | None = None,
) -> Scoreboard:
    """Fold per-run results into per-ticket means plus an overall aggregate row.

    Ticket order follows first appearance in ``results`` so a scoreboard is stable
    and diff-friendly. An empty result set yields an empty scoreboard (no overall).
    ``harness`` (Story 31.1-001 AC1) records the harness every dispatch in
    ``results`` actually ran on; ``None`` (an existing caller that never passes it)
    resolves to the built-in claude default, so today's scoreboard shape is
    unchanged. Accepts ``None`` so a caller can pass ``EvalConfig.harness``
    directly — typed optional at rest, always concrete after
    ``EvalConfig.__post_init__`` pins it. ``provenance`` (Story 31.1-002 AC1) is
    attached as-is; ``None`` (an existing caller that never passes it) keeps the
    scoreboard's provenance-free shape unchanged.
    """
    by_ticket: dict[str, list[RunResult]] = {}
    for r in results:
        by_ticket.setdefault(r.ticket_id, []).append(r)

    tickets = [_score_runs(tid, runs) for tid, runs in by_ticket.items()]
    overall = _score_runs(OVERALL_LABEL, list(results)) if results else None
    return Scoreboard(
        config_name=config_name,
        tickets=tickets,
        overall=overall,
        harness=harness if harness is not None else DEFAULT_HARNESS,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt(value: float | None, *, decimals: int = 1) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_tokens(score: TicketScore) -> str:
    """The token figure with its provenance attached (Story 31.2-002).

    An unavailable figure renders "—" (never 0) and an approximate one — a
    pre-dispatch estimate, an external count, or an aggregate mixing the two — is
    prefixed "~" so it can never be read as a measurement of the same run.
    """
    text = _fmt(score.tokens_mean, decimals=0)
    if score.tokens_mean is not None and score.tokens_source in APPROXIMATE_SOURCES:
        return f"~{text}"
    return text


def _fmt_cost(score: TicketScore) -> str:
    """The dollar figure with its provenance attached (Story 31.2-003).

    ``not_metered`` renders literally — the field case: a local harness's own
    ``cost: 0`` telemetry must never print as a plain "0.0000" indistinguishable
    from a genuinely free hosted run. An approximate source (a $/Mtok estimate
    or a configured local rate) gets the same "~" prefix tokens use.
    """
    if score.cost_source == NOT_METERED:
        return "not metered"
    text = _fmt(score.cost_mean, decimals=4)
    if score.cost_mean is not None and score.cost_source in APPROXIMATE_COST_SOURCES:
        return f"~{text}"
    return text


def render_table(board: Scoreboard) -> str:
    """Render a scoreboard as a fixed-width text table (one row per ticket + overall).

    Story 31.2-001: ``wall_s`` is agent time — any in-process rate-limit wait is
    already excluded and shown separately in ``stalled`` (blank when a ticket
    never stalled), so neither figure silently stands in for the other.

    Story 31.2-002: ``tokens`` is "—" when the harness reports no usage (never 0)
    and "~"-prefixed when the figure is an estimate rather than a measurement.

    Story 31.2-003: ``cost$`` reads "not metered" when the harness has no
    per-token price at all — never "—" (which would read as merely missing)
    and never "0.0000" (which would read as a real, comparable saving).
    """
    header = (
        f"{'ticket':<16} {'runs':>4} {'err':>3} "
        f"{'+LOC':>7} {'-LOC':>7} {'netLOC':>7} "
        f"{'tokens':>9} {'cost$':>8} {'wall_s':>7} {'stalled':>7} {'qual':>5}"
    )
    lines = [f"eval: {board.config_name} (harness: {board.harness})", header, "-" * len(header)]
    rows = list(board.tickets)
    if board.overall is not None:
        rows.append(board.overall)
    for score in rows:
        lines.append(
            f"{score.ticket_id:<16} {score.runs:>4} {score.errors:>3} "
            f"{_fmt(score.loc_added_mean):>7} {_fmt(score.loc_removed_mean):>7} "
            f"{_fmt(score.loc_net_mean):>7} "
            f"{_fmt_tokens(score):>9} "
            f"{_fmt_cost(score):>8} "
            f"{_fmt(score.wall_mean):>7} {_fmt(score.stall_mean):>7} "
            f"{_fmt_rate(score.quality_pass_rate):>5}"
        )
    if any(score.tokens_source in APPROXIMATE_SOURCES for score in rows):
        lines.append(
            "~tokens is an estimate, not a measurement — do not compare it "
            "against a measured figure."
        )
    if any(score.cost_source == NOT_METERED for score in rows):
        lines.append(
            "cost 'not metered' means the harness has no per-token price "
            "(e.g. local inference) — never read it as $0 spent."
        )
    if any(score.cost_source in APPROXIMATE_COST_SOURCES for score in rows):
        lines.append(
            "~cost is derived (a notional $/Mtok estimate or a configured "
            "local rate), not a harness-reported figure."
        )
    return "\n".join(lines)


def _score_to_dict(score: TicketScore) -> dict[str, Any]:
    return {
        "ticket_id": score.ticket_id,
        "runs": score.runs,
        "errors": score.errors,
        "loc_added_mean": score.loc_added_mean,
        "loc_removed_mean": score.loc_removed_mean,
        "loc_net_mean": score.loc_net_mean,
        "tokens_mean": score.tokens_mean,
        "cost_mean": score.cost_mean,
        "wall_mean": score.wall_mean,
        "quality_pass_rate": score.quality_pass_rate,
        "stall_mean": score.stall_mean,
        "input_tokens_mean": score.input_tokens_mean,
        "output_tokens_mean": score.output_tokens_mean,
        "cache_read_tokens_mean": score.cache_read_tokens_mean,
        "cache_creation_tokens_mean": score.cache_creation_tokens_mean,
        "tokens_source": score.tokens_source,
        "cost_source": score.cost_source,
    }


def _provenance_to_dict(p: Provenance) -> dict[str, Any]:
    return {
        "harness": p.harness,
        "model": p.model,
        "harness_version": p.harness_version,
        "host": p.host,
        "config_name": p.config_name,
        "seed": p.seed,
        "ticket_ids": list(p.ticket_ids),
        "n": p.n,
        "timestamp": p.timestamp,
        "cost_metered": p.cost_metered,
        "local_rate_usd_per_million_tokens": p.local_rate_usd_per_million_tokens,
    }


def scoreboard_to_dict(board: Scoreboard) -> dict[str, Any]:
    """Serialise a scoreboard to a plain dict for JSON output / baseline storage.

    The ``provenance`` key (Story 31.1-002 AC1) is present only when the board
    carries one — a scoreboard built without it (an older caller, a legacy
    baseline file) keeps exactly today's shape, so this stays backward
    compatible with every scoreboard on disk.
    """
    payload: dict[str, Any] = {
        "config_name": board.config_name,
        "harness": board.harness,
        "tickets": [_score_to_dict(t) for t in board.tickets],
        "overall": _score_to_dict(board.overall) if board.overall else None,
    }
    if board.provenance is not None:
        payload["provenance"] = _provenance_to_dict(board.provenance)
    return payload


# ---------------------------------------------------------------------------
# The isolation runner
# ---------------------------------------------------------------------------


# A dispatcher is anything with dispatch_agent's keyword surface; tests inject a
# fake that edits ``cwd`` and returns a canned AgentResult instead of a live model.
Dispatcher = Callable[..., AgentResult]


# ---------------------------------------------------------------------------
# Harness selection (Story 31.1-001)
# ---------------------------------------------------------------------------


def resolve_eval_harness(
    config: EvalConfig,
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    probe_runner: ProbeRunner | None = None,
) -> HarnessConfig:
    """Resolve and preflight the eval's harness before any ticket dispatches.

    Every ticket dispatch must go through the same resolved harness (command +
    parser), so this resolves once, up front — never per-ticket, never mid-run.
    A config naming no harness pins ``config.harness`` to
    :data:`sdlc.harness.DEFAULT_HARNESS` in ``EvalConfig.__post_init__`` (mirroring
    the model pin), so the no-harness path never reaches the registry lookup, the
    probe, or the model-pin check below — dispatch stays byte-identical to today
    (AC3).

    Raises :class:`EvalConfigError` — before any dispatch, no half-run — when:

    - the name is absent from the registry, or no registry is configured (AC4);
    - a registry entry is declared ``enabled: false`` (AC4);
    - the harness's ``probe`` command fails, meaning its CLI is not installed /
      authenticated on this machine (AC5);
    - a registry harness's command carries no ``{model}`` placeholder, so it
      cannot honour the eval's pinned ``model`` (AC6) — surfaced here rather than
      silently dropped.
    """
    try:
        harness = resolve_harness(config.harness, config_path=config_path, env=env)
    except HarnessError as exc:
        where = f" (registry: {config_path})" if config_path is not None else ""
        raise EvalConfigError(f"{exc}{where}") from exc

    if not harness.enabled:
        raise EvalConfigError(
            f"harness {harness.name!r} is disabled in the registry "
            f"({config_path}); enable it there or choose another harness"
        )

    probe = probe_harness(harness, runner=probe_runner)
    if probe.status is ProbeStatus.UNAVAILABLE:
        raise EvalConfigError(
            f"harness {harness.name!r} probe failed: "
            f"{probe.detail or 'CLI unavailable'}"
        )

    if harness.source == "registry" and "{model}" not in harness.command:
        raise EvalConfigError(
            f"harness {harness.name!r} cannot take a model pin (config model="
            f"{config.model!r}); add a {{model}} placeholder and a 'models' map "
            f"to its entry in the harness registry, or drop the harness override "
            f"to run on the default claude harness"
        )

    return harness


def dispatcher_for_harness(harness: HarnessConfig) -> Dispatcher:
    """Bind a resolved harness's argv + parser to :func:`run_eval`'s Dispatcher seam.

    Every ticket then dispatches through :func:`sdlc.harness.dispatch_on_harness`
    bound to this one already-resolved ``harness`` — the registry is never
    re-consulted inside the run loop. For the built-in/``env`` Claude slot this is
    byte-identical to passing no dispatcher at all (AC3): ``dispatch_on_harness``
    renders the same argv :func:`sdlc.dispatch.dispatch_agent` would resolve on its
    own and keeps the default (stream-json) parser.
    """
    return functools.partial(dispatch_on_harness, harness)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_workspace(template: Path, dest: Path) -> None:
    """Copy ``template`` into ``dest`` and commit it as a clean git baseline.

    The copy + ``git init`` is what keeps the eval in isolation: the agent edits a
    throwaway clone, never the framework repo, and the diff is measured against
    this committed baseline.
    """
    shutil.copytree(template, dest)
    _git(dest, "init", "-q")
    # Isolate from any global/repo hooks so a baseline commit is deterministic.
    no_hooks = dest.parent / ".no-hooks"
    _git(dest, "config", "core.hooksPath", str(no_hooks))
    _git(dest, "config", "user.email", "eval@fxmartin.me")
    _git(dest, "config", "user.name", "sdlc-eval")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--no-verify", "-m", "chore: eval baseline")


def _measure_diff(cwd: Path) -> DiffStats:
    """LOC delta of the working tree vs the baseline commit, new files included."""
    _git(cwd, "add", "-A")
    numstat = _git(cwd, "diff", "--cached", "--numstat").stdout
    return parse_diff_numstat(numstat)


def run_ticket(
    ticket: Ticket,
    config: EvalConfig,
    run_index: int,
    workspace: Path,
    *,
    dispatcher: Dispatcher = dispatch_agent,
    timeout: int = DEFAULT_TICKET_TIMEOUT_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    rate_limit_max_wait_s: int = DEFAULT_RATE_LIMIT_MAX_WAIT_S,
    capabilities: Mapping[str, bool] | None = None,
    metered: bool = True,
    local_rate_usd_per_million_tokens: float | None = None,
) -> RunResult:
    """Drive one ticket once in an isolated workspace and score the result.

    Materialises a fresh git baseline from the config's sample target, dispatches
    the agent headlessly into it, then scores the diff (LOC), token/cost usage,
    wall-time, and the optional quality check. A dispatch failure is captured as
    ``error`` (with a zero diff) so one bad run never aborts the eval.

    Story 31.2-001: a :class:`RateLimitError` mid-dispatch is a recoverable,
    time-based pause (mirroring build.py's in-process wait, Story 14.1-003) —
    the ticket waits for the window to reopen and retries the *same* dispatch,
    rather than being scored as a lost run. Time spent waiting is tracked apart
    from ``wall_s`` (see ``RunResult.stall_s``) so a single throttled ticket
    can't skew ``wall_mean`` across the scoreboard. A wait beyond
    ``rate_limit_max_wait_s`` gives up and scores the ticket as an error — an
    eval sweep has no durable-park/resume path like a build run does.

    Story 31.2-002: ``capabilities`` is the resolved harness capability map (see
    :func:`sdlc.capability.resolve_capabilities`). A harness that does not declare
    ``usage_tracking`` scores ``tokens``/``cost_usd`` as *unavailable* rather than
    the numbers it happened to print — and never as 0. ``None`` (no map resolved)
    keeps today's behaviour for the built-in dispatch seam.

    Story 31.2-003: ``metered``/``local_rate_usd_per_million_tokens`` are the
    resolved harness's own cost-provenance fields (see
    :class:`sdlc.harness.HarnessConfig`). Both default to today's hosted
    assumption, so a caller that never passes them keeps unchanged behaviour.
    """
    workdir = workspace / f"{ticket.id}-{run_index}"
    _init_workspace(config.target, workdir)

    # Issue #435: append the schema-derived result-block contract to the bare
    # ticket prompt so a live agent ends with the structured block dispatch
    # validates against — instead of prose that fails validation and discards the
    # run's tokens/cost/quality. The pinned ``config.model`` (never None by
    # default) is threaded through so the eval never runs on the CLI default.
    prompt = ticket.prompt + "\n\n" + _result_wrapper(AGENT_SCHEMAS[config.agent_type])

    start = time.monotonic()
    stall_s = 0.0
    while True:
        try:
            result = dispatcher(
                config.agent_type,
                prompt,
                cwd=workdir,
                model=config.model,
                timeout=timeout,
            )
        except RateLimitError as exc:
            wait_s = seconds_until_reset(
                exc.signal, now=time.time(), window_s=rate_limit_max_wait_s
            )
            if not within_wait_cap(wait_s, rate_limit_max_wait_s):
                wall = max(0.0, time.monotonic() - start - stall_s)
                return RunResult(
                    ticket_id=ticket.id,
                    run_index=run_index,
                    diff=_measure_diff(workdir),
                    wall_s=wall,
                    error=(
                        f"rate limit wait ({wait_s}s) exceeds the "
                        f"{rate_limit_max_wait_s}s cap: {exc}"
                    ),
                    status="error",
                    stall_s=stall_s or None,
                )
            sleep_fn(wait_s)
            stall_s += wait_s
            continue
        except ContractError as exc:
            # A contract miss is not a lost run: the agent still edited the
            # workspace and burned tokens. Score the diff and quality, and
            # recover tokens/cost from the telemetry parsers.py attached to the
            # exception, under a distinct status so the scoreboard separates a
            # scored miss from a real error.
            wall = max(0.0, time.monotonic() - start - stall_s)
            breakdown = harness_breakdown(
                getattr(exc, "usage", None),
                capabilities=capabilities,
                harness=config.harness,
            )
            reported_cost = (
                getattr(exc, "cost_usd", None)
                if usage_is_tracked(capabilities)
                else None
            )
            cost_value, cost_source = _cost_from(
                reported_cost,
                breakdown,
                usd_per_million_tokens=config.usd_per_million_tokens,
                metered=metered,
                local_rate_usd_per_million_tokens=local_rate_usd_per_million_tokens,
            )
            return RunResult(
                ticket_id=ticket.id,
                run_index=run_index,
                diff=_measure_diff(workdir),
                wall_s=wall,
                tokens=breakdown.total,
                cost_usd=cost_value,
                quality_pass=run_quality_check(ticket.quality_cmd, workdir),
                status="contract_miss",
                stall_s=stall_s or None,
                usage=breakdown,
                cost_source=cost_source,
            )
        except Exception as exc:  # noqa: BLE001 — record any dispatch failure, keep going
            wall = max(0.0, time.monotonic() - start - stall_s)
            return RunResult(
                ticket_id=ticket.id,
                run_index=run_index,
                diff=_measure_diff(workdir),
                wall_s=wall,
                error=f"{type(exc).__name__}: {exc}",
                status="error",
                stall_s=stall_s or None,
            )
        break
    wall = max(0.0, time.monotonic() - start - stall_s)
    breakdown = harness_breakdown(
        result.usage, capabilities=capabilities, harness=config.harness
    )
    # ``usage_tracking`` covers cost as well as tokens: a harness that does not
    # declare it reports neither, so the arm is never priced off a figure it was
    # not entitled to give.
    reported_cost = result.cost_usd if usage_is_tracked(capabilities) else None
    cost_value, cost_source = _cost_from(
        reported_cost,
        breakdown,
        usd_per_million_tokens=config.usd_per_million_tokens,
        metered=metered,
        local_rate_usd_per_million_tokens=local_rate_usd_per_million_tokens,
    )

    return RunResult(
        ticket_id=ticket.id,
        run_index=run_index,
        diff=_measure_diff(workdir),
        wall_s=wall,
        tokens=breakdown.total,
        cost_usd=cost_value,
        quality_pass=run_quality_check(ticket.quality_cmd, workdir),
        error=None,
        stall_s=stall_s or None,
        usage=breakdown,
        cost_source=cost_source,
    )


def run_eval(
    config: EvalConfig,
    workspace: Path,
    *,
    dispatcher: Dispatcher = dispatch_agent,
    timeout: int = DEFAULT_TICKET_TIMEOUT_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    rate_limit_max_wait_s: int = DEFAULT_RATE_LIMIT_MAX_WAIT_S,
    capabilities: Mapping[str, bool] | None = None,
    metered: bool = True,
    local_rate_usd_per_million_tokens: float | None = None,
) -> list[RunResult]:
    """Run every ticket × ``n`` runs in isolation and return the per-run results.

    Pass the result list to :func:`aggregate` for a scoreboard. ``workspace`` is a
    throwaway directory (the caller owns its lifetime); the framework repo and the
    sample-target template are never mutated. ``capabilities`` (Story 31.2-002) is
    the resolved harness capability map every run's usage is gated on — see
    :func:`run_ticket`. ``metered``/``local_rate_usd_per_million_tokens``
    (Story 31.2-003) are the resolved harness's own cost-provenance fields,
    threaded through unchanged for every run.
    """
    results: list[RunResult] = []
    for ticket in config.tickets:
        for run_index in range(config.n):
            results.append(
                run_ticket(
                    ticket,
                    config,
                    run_index,
                    workspace,
                    dispatcher=dispatcher,
                    timeout=timeout,
                    sleep_fn=sleep_fn,
                    rate_limit_max_wait_s=rate_limit_max_wait_s,
                    capabilities=capabilities,
                    metered=metered,
                    local_rate_usd_per_million_tokens=local_rate_usd_per_million_tokens,
                )
            )
    return results
