# ABOUTME: Variant comparison + regression baselines (Story 18.1-002) — a thin layer
# ABOUTME: over the eval harness: diff two scoreboards, verdict per ticket, flag regressions.

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdlc.harness import DEFAULT_HARNESS

# Default relative tolerance: a metric must move more than this fraction of its
# baseline to count as improved/regressed. Below it, model-run variance swamps the
# signal, so the change is "neutral" — this is the knob that keeps the false-positive
# rate down (a tiny token wobble is not a regression).
DEFAULT_TOLERANCE = 0.10

# Per-metric direction.
IMPROVED = "improved"
REGRESSED = "regressed"
NEUTRAL = "neutral"

# Per-ticket (and overall) verdict.
BETTER = "better"
WORSE = "worse"
# (NEUTRAL is reused as the neutral verdict.)


class BaselineError(Exception):
    """A baseline/scoreboard file that is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class MetricSpec:
    """One comparable metric: which scoreboard key it reads and its good direction."""

    key: str
    label: str
    lower_is_better: bool


# The comparable means in a :class:`sdlc.evaluate.TicketScore` dict. LOC/tokens/cost/
# wall are "less is better"; quality is the one "more is better" — exactly the signal
# Epic-14 routing must hold (does a cheaper model keep quality up while cutting cost?).
# Story 31.2-001: "wall_mean" is stall-adjusted agent time — any in-process
# rate-limit wait a run recorded is already excluded (see
# ``sdlc.evaluate.RunResult.stall_s``) — so a rate-limited hosted harness is
# compared on agent speed, not the state of its quota.
COMPARED_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("loc_net_mean", "netLOC", lower_is_better=True),
    MetricSpec("tokens_mean", "tokens", lower_is_better=True),
    MetricSpec("cost_mean", "cost$", lower_is_better=True),
    MetricSpec("wall_mean", "wall_s", lower_is_better=True),
    MetricSpec("quality_pass_rate", "qual", lower_is_better=False),
)

# The quality metric drives the verdict: a quality drop is never "better", however
# much cheaper the run got.
_QUALITY_KEY = "quality_pass_rate"


@dataclass(frozen=True)
class MetricDelta:
    """One metric compared across two scoreboards: values, delta, and a direction."""

    key: str
    label: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    pct: float | None
    direction: str


@dataclass(frozen=True)
class TicketDelta:
    """A ticket's per-metric deltas folded into one better/worse/neutral verdict."""

    ticket_id: str
    metrics: list[MetricDelta]
    verdict: str


@dataclass(frozen=True)
class Comparison:
    """A full A/B comparison: per-ticket deltas + an overall row, at a tolerance."""

    baseline_name: str
    candidate_name: str
    tolerance: float
    tickets: list[TicketDelta]
    overall: TicketDelta | None
    # Story 31.1-002 AC3: recorded unconditionally (not only when they differ)
    # so a cross-harness A/B is never misread as a model-only delta — the
    # header always states both, matching or not.
    baseline_harness: str = DEFAULT_HARNESS
    candidate_harness: str = DEFAULT_HARNESS


# ---------------------------------------------------------------------------
# Classification (pure — the unit-tested core)
# ---------------------------------------------------------------------------


def classify_metric(
    spec: MetricSpec,
    baseline: float | None,
    candidate: float | None,
    tolerance: float,
) -> MetricDelta:
    """Classify one metric's move as improved / regressed / neutral.

    A missing value on either side yields ``NEUTRAL`` with no delta (the metric
    is not comparable, never a false regression). When the baseline is non-zero the
    change is measured *relatively* against ``tolerance``; a zero baseline can't be —
    so any non-zero move from zero is a categorical change and flags (``pct`` stays
    ``None``). ``delta`` is always ``candidate - baseline``.
    """
    if baseline is None or candidate is None:
        return MetricDelta(spec.key, spec.label, baseline, candidate, None, None, NEUTRAL)

    delta = candidate - baseline
    pct: float | None
    if baseline != 0:
        rel = delta / abs(baseline)
        pct = rel
        beyond = abs(rel) > tolerance
    else:
        pct = None
        beyond = delta != 0

    if not beyond:
        direction = NEUTRAL
    else:
        improved = (delta < 0) if spec.lower_is_better else (delta > 0)
        direction = IMPROVED if improved else REGRESSED

    return MetricDelta(spec.key, spec.label, baseline, candidate, delta, pct, direction)


def ticket_verdict(metrics: list[MetricDelta]) -> str:
    """Fold per-metric directions into a single ``BETTER``/``WORSE``/``NEUTRAL`` verdict.

    Quality is decisive: a quality regression is ``WORSE`` and a quality improvement
    is ``BETTER``, whatever the efficiency metrics did (we never trade quality for
    speed). With quality neutral or absent, the efficiency metrics (LOC/tokens/cost/
    wall) are tallied — more improvements than regressions is ``BETTER``, the reverse
    ``WORSE``, a tie ``NEUTRAL``.
    """
    quality = next((m for m in metrics if m.key == _QUALITY_KEY), None)
    if quality is not None:
        if quality.direction == REGRESSED:
            return WORSE
        if quality.direction == IMPROVED:
            return BETTER

    efficiency = [m for m in metrics if m.key != _QUALITY_KEY]
    improved = sum(1 for m in efficiency if m.direction == IMPROVED)
    regressed = sum(1 for m in efficiency if m.direction == REGRESSED)
    if improved > regressed:
        return BETTER
    if regressed > improved:
        return WORSE
    return NEUTRAL


# ---------------------------------------------------------------------------
# Scoreboard comparison
# ---------------------------------------------------------------------------


def _metric_value(score: dict[str, Any] | None, key: str) -> float | None:
    if not score:
        return None
    value = score.get(key)
    return None if value is None else float(value)


def _compare_score(
    ticket_id: str,
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    tolerance: float,
) -> TicketDelta:
    metrics = [
        classify_metric(
            spec,
            _metric_value(baseline, spec.key),
            _metric_value(candidate, spec.key),
            tolerance,
        )
        for spec in COMPARED_METRICS
    ]
    return TicketDelta(ticket_id=ticket_id, metrics=metrics, verdict=ticket_verdict(metrics))


def _index_tickets(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        t["ticket_id"]: t
        for t in board.get("tickets", [])
        if isinstance(t, dict) and "ticket_id" in t
    }


def compare_scoreboards(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    tolerance: float = DEFAULT_TOLERANCE,
) -> Comparison:
    """Diff two scoreboard dicts (the ``scoreboard_to_dict`` / baseline-file shape).

    Tickets are matched by id; ordering follows the baseline, with any
    candidate-only tickets appended (each scored neutral — no comparable baseline).
    The two ``overall`` rows are compared into a single overall verdict.
    """
    base_idx = _index_tickets(baseline)
    cand_idx = _index_tickets(candidate)

    ordered_ids = list(base_idx)
    ordered_ids += [tid for tid in cand_idx if tid not in base_idx]

    tickets = [
        _compare_score(tid, base_idx.get(tid), cand_idx.get(tid), tolerance)
        for tid in ordered_ids
    ]

    base_overall = baseline.get("overall")
    cand_overall = candidate.get("overall")
    overall = (
        _compare_score("OVERALL", base_overall, cand_overall, tolerance)
        if (base_overall or cand_overall)
        else None
    )

    return Comparison(
        baseline_name=str(baseline.get("config_name", "baseline")),
        candidate_name=str(candidate.get("config_name", "candidate")),
        tolerance=tolerance,
        tickets=tickets,
        overall=overall,
        baseline_harness=str(baseline.get("harness") or DEFAULT_HARNESS),
        candidate_harness=str(candidate.get("harness") or DEFAULT_HARNESS),
    )


# ---------------------------------------------------------------------------
# Provenance-aware guardrails (Story 31.1-002)
# ---------------------------------------------------------------------------


def _board_ticket_ids(board: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        str(t["ticket_id"])
        for t in board.get("tickets", [])
        if isinstance(t, dict) and "ticket_id" in t
    )


def ticket_set_mismatches(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Human-readable reasons two scoreboards' ticket sets differ, ``[]`` if they match.

    A "ticket set" is identified by config name + seed + ticket ids (AC2).
    Config name and ticket ids are always present (even on a legacy
    scoreboard, whose ``tickets`` list already names every ticket that ran),
    so those two checks apply unconditionally. Seed only lives inside the
    ``provenance`` block (Story 31.1-002 AC1), so it is compared only when
    *both* sides carry one — a legacy scoreboard's absent seed is never
    manufactured into a mismatch (AC4: legacy boards are accepted, not
    penalised for predating provenance).
    """
    reasons: list[str] = []

    base_name = str(baseline.get("config_name", ""))
    cand_name = str(candidate.get("config_name", ""))
    if base_name != cand_name:
        reasons.append(f"config name differs: {base_name!r} vs {cand_name!r}")

    base_prov = baseline.get("provenance")
    cand_prov = candidate.get("provenance")
    if isinstance(base_prov, dict) and isinstance(cand_prov, dict):
        base_seed = base_prov.get("seed")
        cand_seed = cand_prov.get("seed")
        if base_seed != cand_seed:
            reasons.append(f"seed differs: {base_seed!r} vs {cand_seed!r}")

    base_ids = _board_ticket_ids(baseline)
    cand_ids = _board_ticket_ids(candidate)
    if base_ids != cand_ids:
        only_base = sorted(base_ids - cand_ids)
        only_cand = sorted(cand_ids - base_ids)
        detail = []
        if only_base:
            detail.append(f"only in baseline: {only_base}")
        if only_cand:
            detail.append(f"only in candidate: {only_cand}")
        reasons.append("ticket ids differ" + (f" ({'; '.join(detail)})" if detail else ""))

    return reasons


def provenance_warnings(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Warn (never reject, AC4) about a scoreboard predating provenance tracking."""
    warnings: list[str] = []
    if "provenance" not in baseline:
        name = baseline.get("config_name", "baseline")
        warnings.append(
            f"{name}: provenance unknown (legacy scoreboard predates provenance tracking)"
        )
    if "provenance" not in candidate:
        name = candidate.get("config_name", "candidate")
        warnings.append(
            f"{name}: provenance unknown (legacy scoreboard predates provenance tracking)"
        )
    return warnings


def regressions(comparison: Comparison) -> list[tuple[str, MetricDelta]]:
    """Every regressed metric across all ticket rows plus the overall row.

    Each entry pairs the row's ticket id with the offending :class:`MetricDelta`,
    so a baseline check can report *what* got worse and *where*.
    """
    rows = list(comparison.tickets)
    if comparison.overall is not None:
        rows.append(comparison.overall)
    return [
        (row.ticket_id, m)
        for row in rows
        for m in row.metrics
        if m.direction == REGRESSED
    ]


def has_regressions(comparison: Comparison) -> bool:
    """``True`` when any metric regressed beyond tolerance (the baseline gate signal)."""
    return bool(regressions(comparison))


# ---------------------------------------------------------------------------
# Rendering + serialization
# ---------------------------------------------------------------------------


def _fmt(value: float | None, *, decimals: int = 1) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    return f"{pct * 100:+.0f}%"


_ARROW = {IMPROVED: "↓ better", REGRESSED: "↑ worse", NEUTRAL: "= same"}


def render_comparison_table(comparison: Comparison) -> str:
    """Render an A/B comparison as a text table: per ticket, each metric + a verdict."""
    lines = [
        f"compare: {comparison.baseline_name} (baseline, harness={comparison.baseline_harness}) vs "
        f"{comparison.candidate_name} (candidate, harness={comparison.candidate_harness})  "
        f"[tolerance {comparison.tolerance:.0%}]",
        "wall_s is stall-adjusted agent time (rate-limit waits excluded).",
    ]
    rows = list(comparison.tickets)
    if comparison.overall is not None:
        rows.append(comparison.overall)
    for row in rows:
        lines.append(f"\n{row.ticket_id}: {row.verdict.upper()}")
        for m in row.metrics:
            note = _ARROW.get(m.direction, m.direction)
            lines.append(
                f"  {m.label:<8} {_fmt(m.baseline, decimals=4):>10} -> "
                f"{_fmt(m.candidate, decimals=4):>10}  "
                f"({_fmt_pct(m.pct):>5}) {note}"
            )
    return "\n".join(lines)


def _metric_to_dict(m: MetricDelta) -> dict[str, Any]:
    return {
        "key": m.key,
        "label": m.label,
        "baseline": m.baseline,
        "candidate": m.candidate,
        "delta": m.delta,
        "pct": m.pct,
        "direction": m.direction,
    }


def _ticket_to_dict(t: TicketDelta) -> dict[str, Any]:
    return {
        "ticket_id": t.ticket_id,
        "verdict": t.verdict,
        "metrics": [_metric_to_dict(m) for m in t.metrics],
    }


def comparison_to_dict(comparison: Comparison) -> dict[str, Any]:
    """Serialise a comparison to a plain dict for JSON output / a recorded decision."""
    return {
        "baseline_name": comparison.baseline_name,
        "candidate_name": comparison.candidate_name,
        "tolerance": comparison.tolerance,
        "tickets": [_ticket_to_dict(t) for t in comparison.tickets],
        "overall": _ticket_to_dict(comparison.overall) if comparison.overall else None,
    }


# ---------------------------------------------------------------------------
# Baseline IO
# ---------------------------------------------------------------------------


def load_scoreboard(path: Path) -> dict[str, Any]:
    """Load a scoreboard / baseline JSON file, raising :class:`BaselineError` on any fault.

    Accepts the ``scoreboard_to_dict`` shape (what ``sdlc eval --json`` emits and a
    committed baseline stores); unknown extra keys (e.g. a ``_note``) are tolerated.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineError(f"scoreboard not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BaselineError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BaselineError(f"scoreboard must be a mapping, got {type(raw).__name__}")
    return raw


def save_scoreboard(board: dict[str, Any], path: Path) -> None:
    """Persist a scoreboard dict as pretty JSON (a baseline or a recorded result)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
