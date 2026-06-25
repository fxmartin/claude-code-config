# ABOUTME: Reproducible agentic eval harness (Story 18.1-001) — drives an agent
# ABOUTME: headlessly over a fixed ticket set on a sample repo and scores the diff.

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sdlc.cost_estimate import DEFAULT_USD_PER_MILLION_TOKENS, notional_cost
from sdlc.dispatch import AgentResult, dispatch_agent

# The four usage keys the agent envelope carries (mirrors build._RESULT_USAGE_KEYS
# and dispatch's envelope parsing) so eval token counts match ledger metrics.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Default per-story headless dispatch ceiling (seconds). An eval ticket is a small
# edit on a tiny repo, so it should finish well inside a build's full timeout.
DEFAULT_TICKET_TIMEOUT_S = 600

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


@dataclass(frozen=True)
class Scoreboard:
    """The full eval result: one :class:`TicketScore` per ticket plus an overall."""

    config_name: str
    tickets: list[TicketScore] = field(default_factory=list)
    overall: TicketScore | None = None


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
    if not usage:
        return None
    vals = [usage.get(key) for key in _USAGE_KEYS]
    if all(v is None for v in vals):
        return None
    return sum(int(v or 0) for v in vals)


def result_cost(
    result: AgentResult,
    *,
    usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS,
) -> float | None:
    """Notional cost of a run: the envelope ``cost_usd`` if present, else derived.

    Falls back to a notional figure computed from total tokens (mirrors the
    controller's notional-$ convention) so a run still carries a comparable cost
    even when the agent envelope omits ``total_cost_usd``. ``None`` when neither a
    cost nor any token usage is available.
    """
    if result.cost_usd is not None:
        return float(result.cost_usd)
    tokens = tokens_from_usage(result.usage)
    if tokens is None:
        return None
    return notional_cost(tokens, usd_per_million_tokens=usd_per_million_tokens)


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


def _score_runs(ticket_id: str, runs: Sequence[RunResult]) -> TicketScore:
    quality = [r.quality_pass for r in runs if r.quality_pass is not None]
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
    )


def aggregate(results: Sequence[RunResult], config_name: str) -> Scoreboard:
    """Fold per-run results into per-ticket means plus an overall aggregate row.

    Ticket order follows first appearance in ``results`` so a scoreboard is stable
    and diff-friendly. An empty result set yields an empty scoreboard (no overall).
    """
    by_ticket: dict[str, list[RunResult]] = {}
    for r in results:
        by_ticket.setdefault(r.ticket_id, []).append(r)

    tickets = [_score_runs(tid, runs) for tid, runs in by_ticket.items()]
    overall = _score_runs(OVERALL_LABEL, list(results)) if results else None
    return Scoreboard(config_name=config_name, tickets=tickets, overall=overall)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt(value: float | None, *, decimals: int = 1) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_table(board: Scoreboard) -> str:
    """Render a scoreboard as a fixed-width text table (one row per ticket + overall)."""
    header = (
        f"{'ticket':<16} {'runs':>4} {'err':>3} "
        f"{'+LOC':>7} {'-LOC':>7} {'netLOC':>7} "
        f"{'tokens':>9} {'cost$':>8} {'wall_s':>7} {'qual':>5}"
    )
    lines = [f"eval: {board.config_name}", header, "-" * len(header)]
    rows = list(board.tickets)
    if board.overall is not None:
        rows.append(board.overall)
    for score in rows:
        lines.append(
            f"{score.ticket_id:<16} {score.runs:>4} {score.errors:>3} "
            f"{_fmt(score.loc_added_mean):>7} {_fmt(score.loc_removed_mean):>7} "
            f"{_fmt(score.loc_net_mean):>7} "
            f"{_fmt(score.tokens_mean, decimals=0):>9} "
            f"{_fmt(score.cost_mean, decimals=4):>8} "
            f"{_fmt(score.wall_mean):>7} {_fmt_rate(score.quality_pass_rate):>5}"
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
    }


def scoreboard_to_dict(board: Scoreboard) -> dict[str, Any]:
    """Serialise a scoreboard to a plain dict for JSON output / baseline storage."""
    return {
        "config_name": board.config_name,
        "tickets": [_score_to_dict(t) for t in board.tickets],
        "overall": _score_to_dict(board.overall) if board.overall else None,
    }


# ---------------------------------------------------------------------------
# The isolation runner
# ---------------------------------------------------------------------------


# A dispatcher is anything with dispatch_agent's keyword surface; tests inject a
# fake that edits ``cwd`` and returns a canned AgentResult instead of a live model.
Dispatcher = Callable[..., AgentResult]


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
) -> RunResult:
    """Drive one ticket once in an isolated workspace and score the result.

    Materialises a fresh git baseline from the config's sample target, dispatches
    the agent headlessly into it, then scores the diff (LOC), token/cost usage,
    wall-time, and the optional quality check. A dispatch failure is captured as
    ``error`` (with a zero diff) so one bad run never aborts the eval.
    """
    workdir = workspace / f"{ticket.id}-{run_index}"
    _init_workspace(config.target, workdir)

    start = time.monotonic()
    try:
        result = dispatcher(
            config.agent_type,
            ticket.prompt,
            cwd=workdir,
            timeout=timeout,
        )
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — record any dispatch failure, keep going
        wall = time.monotonic() - start
        return RunResult(
            ticket_id=ticket.id,
            run_index=run_index,
            diff=_measure_diff(workdir),
            wall_s=wall,
            error=f"{type(exc).__name__}: {exc}",
        )
    wall = time.monotonic() - start

    return RunResult(
        ticket_id=ticket.id,
        run_index=run_index,
        diff=_measure_diff(workdir),
        wall_s=wall,
        tokens=tokens_from_usage(result.usage),
        cost_usd=result_cost(
            result, usd_per_million_tokens=config.usd_per_million_tokens
        ),
        quality_pass=run_quality_check(ticket.quality_cmd, workdir),
        error=error,
    )


def run_eval(
    config: EvalConfig,
    workspace: Path,
    *,
    dispatcher: Dispatcher = dispatch_agent,
    timeout: int = DEFAULT_TICKET_TIMEOUT_S,
) -> list[RunResult]:
    """Run every ticket × ``n`` runs in isolation and return the per-run results.

    Pass the result list to :func:`aggregate` for a scoreboard. ``workspace`` is a
    throwaway directory (the caller owns its lifetime); the framework repo and the
    sample-target template are never mutated.
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
                )
            )
    return results
