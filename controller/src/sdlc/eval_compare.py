# ABOUTME: Variant comparison + regression baselines (Story 18.1-002) — a thin layer
# ABOUTME: over the eval harness: diff two scoreboards, verdict per ticket, flag regressions.

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdlc.harness import DEFAULT_HARNESS
from sdlc.usage import (
    APPROXIMATE_SOURCES,
    DEFAULT_MIX_TOLERANCE,
    MEASURED,
    UNAVAILABLE,
    USAGE_COMPONENTS,
    describe_mix,
    mix_divergence,
)

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

# The two axes made of token usage. They stand or fall together: ``cost_mean`` is
# derived from the same components as ``tokens_mean``, so a token mix the arms do
# not share makes the dollar figure just as incomparable (Story 31.2-002).
USAGE_METRIC_KEYS: tuple[str, ...] = ("tokens_mean", "cost_mean")

# Scoreboard key holding each component's mean, by canonical component name.
_COMPONENT_KEYS: dict[str, str] = {
    "input": "input_tokens_mean",
    "output": "output_tokens_mean",
    "cache_read": "cache_read_tokens_mean",
    "cache_creation": "cache_creation_tokens_mean",
}


@dataclass(frozen=True)
class MetricDelta:
    """One metric compared across two scoreboards: values, delta, and a direction.

    Story 31.2-002: ``comparable`` is False when the two arms' figures are not made
    of the same thing — a figure missing on one side, an estimate against a
    measurement, or two token totals with materially different component mixes.
    ``reason`` says which. A non-comparable metric is always ``NEUTRAL`` and is
    excluded from the verdict, so an axis nobody could judge never reads as a pass.
    """

    key: str
    label: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    pct: float | None
    direction: str
    comparable: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class TicketDelta:
    """A ticket's per-metric deltas folded into one better/worse/neutral verdict.

    ``excluded`` names (by label) every axis the verdict could not use, so a
    partially-comparable comparison always states what it left out.
    """

    ticket_id: str
    metrics: list[MetricDelta]
    verdict: str
    excluded: tuple[str, ...] = ()


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
    *,
    reason: str | None = None,
) -> MetricDelta:
    """Classify one metric's move as improved / regressed / neutral.

    A missing value on either side yields ``NEUTRAL`` with no delta (the metric
    is not comparable, never a false regression). When the baseline is non-zero the
    change is measured *relatively* against ``tolerance``; a zero baseline can't be —
    so any non-zero move from zero is a categorical change and flags (``pct`` stays
    ``None``). ``delta`` is always ``candidate - baseline``.

    Story 31.2-002: ``reason`` marks the metric not-comparable up front — the two
    arms' figures exist but are not made of the same thing (see
    :func:`usage_comparability`). Such a metric keeps its raw values for display,
    carries no delta, and is excluded from the verdict.
    """
    if reason is not None:
        return MetricDelta(
            spec.key, spec.label, baseline, candidate, None, None, NEUTRAL,
            comparable=False, reason=reason,
        )
    if baseline is None or candidate is None:
        side = "baseline" if baseline is None else "candidate"
        return MetricDelta(
            spec.key, spec.label, baseline, candidate, None, None, NEUTRAL,
            comparable=False,
            reason=f"{spec.label} unavailable on the {side} — a missing figure is not zero",
        )

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


def excluded_labels(metrics: list[MetricDelta]) -> tuple[str, ...]:
    """The labels of every axis the verdict could not use, in metric order."""
    return tuple(m.label for m in metrics if not m.comparable)


def ticket_verdict(metrics: list[MetricDelta]) -> str:
    """Fold per-metric directions into a single ``BETTER``/``WORSE``/``NEUTRAL`` verdict.

    Quality is decisive: a quality regression is ``WORSE`` and a quality improvement
    is ``BETTER``, whatever the efficiency metrics did (we never trade quality for
    speed). With quality neutral or absent, the efficiency metrics (LOC/tokens/cost/
    wall) are tallied — more improvements than regressions is ``BETTER``, the reverse
    ``WORSE``, a tie ``NEUTRAL``.

    Story 31.2-002: only *comparable* metrics are counted. An axis the two arms
    cannot be judged on contributes nothing in either direction — it can neither
    tip the verdict nor be read as a pass — and the verdict is computed from
    whatever is left. With nothing comparable at all the verdict is ``NEUTRAL``.
    """
    metrics = [m for m in metrics if m.comparable]
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


def _component_mix(score: dict[str, Any] | None) -> dict[str, float] | None:
    """The component mix of a scoreboard row's token means, or ``None``.

    ``None`` when the row carries no component breakdown at all (a scoreboard
    written before Story 31.2-002) or when the components sum to zero — in both
    cases there is no mix to judge, which the caller must report rather than
    assume away.
    """
    values = {
        name: _metric_value(score, key) for name, key in _COMPONENT_KEYS.items()
    }
    if all(v is None for v in values.values()):
        return None
    total = sum(v or 0.0 for v in values.values())
    if total <= 0:
        return None
    return {name: (values[name] or 0.0) / total for name in USAGE_COMPONENTS}


def _token_source(score: dict[str, Any] | None) -> str:
    """A row's token provenance, defaulting an unlabelled row to ``MEASURED``.

    Rows written before Story 31.2-002 carry no ``tokens_source``; they recorded
    live usage, so treating them as measured is faithful. Their *mix* is still
    unknown, which :func:`usage_comparability` catches separately.
    """
    if not score:
        return UNAVAILABLE
    value = score.get("tokens_source")
    return str(value) if value else MEASURED


def usage_comparability(
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    mix_tolerance: float = DEFAULT_MIX_TOLERANCE,
) -> str | None:
    """Why the token/cost axes are not comparable across two rows, or ``None``.

    Availability is necessary but not sufficient (the 2026-09-05 field finding):
    two arms can both report and still be describing different work at different
    prices — 15,160 tokens of cache-*creation* against 14,152 of cache-*read* are
    within 7% on the total and nothing alike underneath. So comparability is
    judged on the component breakdown, in three steps:

    1. **Availability** — a figure missing on either side. A missing number is not
       zero, and the arm that cannot report must never look free.
    2. **Provenance** — an estimate (or an external/mixed approximation) against a
       measurement. Different quantities; never silently compared.
    3. **Mix** — the largest per-component share gap beyond ``mix_tolerance``, or a
       row with no component breakdown to judge at all.
    """
    for side, score in (("baseline", baseline), ("candidate", candidate)):
        if _metric_value(score, "tokens_mean") is None:
            return (
                f"token usage unavailable on the {side} "
                f"(source: {_token_source(score)}) — a missing figure is not zero"
            )

    base_source, cand_source = _token_source(baseline), _token_source(candidate)
    if (base_source in APPROXIMATE_SOURCES) != (cand_source in APPROXIMATE_SOURCES):
        return (
            f"baseline is {base_source}, candidate is {cand_source} — an estimate "
            f"is not a measurement of the same run"
        )

    def _no_breakdown(side: str) -> str:
        return (
            f"no component breakdown recorded on the {side}, so the token mix "
            f"cannot be judged — regenerate it with `sdlc eval --json`"
        )

    base_mix = _component_mix(baseline)
    if base_mix is None:
        return _no_breakdown("baseline")
    cand_mix = _component_mix(candidate)
    if cand_mix is None:
        return _no_breakdown("candidate")

    gap = mix_divergence(base_mix, cand_mix)
    if gap > mix_tolerance:
        return (
            f"token mix differs materially: baseline {describe_mix(base_mix)} vs "
            f"candidate {describe_mix(cand_mix)} (largest component gap "
            f"{gap:.0%} > {mix_tolerance:.0%})"
        )
    return None


def _compare_score(
    ticket_id: str,
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    tolerance: float,
) -> TicketDelta:
    usage_reason = usage_comparability(baseline, candidate)
    metrics = [
        classify_metric(
            spec,
            _metric_value(baseline, spec.key),
            _metric_value(candidate, spec.key),
            tolerance,
            reason=usage_reason if spec.key in USAGE_METRIC_KEYS else None,
        )
        for spec in COMPARED_METRICS
    ]
    return TicketDelta(
        ticket_id=ticket_id,
        metrics=metrics,
        verdict=ticket_verdict(metrics),
        excluded=excluded_labels(metrics),
    )


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
    """Render an A/B comparison as a text table: per ticket, each metric + a verdict.

    Story 31.2-002: an axis the two arms cannot be judged on renders "not
    comparable" with its reason instead of a direction arrow — a near-equal total
    built from a different component mix must never print as "= same" — and the
    ticket's verdict line names every axis it had to exclude.
    """
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
        suffix = (
            f"  (excluded from verdict: {', '.join(row.excluded)})"
            if row.excluded
            else ""
        )
        lines.append(f"\n{row.ticket_id}: {row.verdict.upper()}{suffix}")
        for m in row.metrics:
            if not m.comparable:
                lines.append(
                    f"  {m.label:<8} {_fmt(m.baseline, decimals=4):>10} -> "
                    f"{_fmt(m.candidate, decimals=4):>10}  "
                    f"not comparable — {m.reason}"
                )
                continue
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
        "comparable": m.comparable,
        "reason": m.reason,
    }


def _ticket_to_dict(t: TicketDelta) -> dict[str, Any]:
    return {
        "ticket_id": t.ticket_id,
        "verdict": t.verdict,
        "excluded": list(t.excluded),
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
