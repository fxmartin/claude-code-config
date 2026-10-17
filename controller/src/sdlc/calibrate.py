# ABOUTME: Calibrates the escalation bars from the reconciled story totals in the fleet's ledgers.
# ABOUTME: Story 34.5-002 — merge, dedupe, scope to origin, p75 tokens + rework rate, write the YAML.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from sdlc.build import Ledger
from sdlc.model_routing import ROUTING_KEY
from sdlc.predictor import MIN_GLOBAL_SAMPLE

# The heavy tail the token bar marks: a story at/above the fleet's own p75.
TOKENS_QUANTILE = 0.75

# A story is rework-heavy when forecast at least this many times likelier to
# rework than the fleet's observed base rate. Clamped to the predictor's own
# probability bounds, so the bar can neither fire on every story nor on none.
REWORK_MULTIPLE = 2.0
MIN_REWORK_THRESHOLD = 0.05
MAX_REWORK_THRESHOLD = 0.95

_BARS = ("predicted_tokens_threshold", "rework_threshold")


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile of ``values`` by linear interpolation between ranks."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = q * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (rank - low) * (ordered[high] - ordered[low])


def normalize_origin(url: str) -> str:
    """``host/path`` of a git remote, so one repo's http, ssh and scp forms agree.

    Scheme, credentials, port and a trailing ``.git`` are dropped and the rest is
    lower-cased: the same repo is reached as ``http://gitlab.test/...`` here and
    over ssh from another host.
    """
    url = url.strip()
    scp = re.match(r"^[^/@\s]+@([^:/\s]+):(?!//)(.+)$", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    path = path.strip("/").removesuffix(".git")
    return f"{host}/{path}".lower()


@dataclass
class Sample:
    """The merged, deduplicated, origin-scoped reconciled stories."""

    rows: list[dict] = field(default_factory=list)
    ledgers: int = 0
    duplicates: int = 0
    out_of_scope: int = 0


def load_sample(paths: Sequence[Path], *, origin: str | None) -> Sample:
    """Merge the reconciled stories of every ledger in ``paths``.

    A reconciled story is one ``Ledger.prediction_training_rows`` returns (the
    reader the predictor and ``predict-quality`` share): DONE, with measured
    usage. Copies of one ledger — or of the same run on two hosts — count once,
    keyed by ``(run_id, story_id)``.

    With an ``origin``, a run is kept unless its ledger stamped a *different*
    origin on it. A run that stamped none predates origin recording and cannot
    be attributed elsewhere; a ledger is a repo's own file, so it is kept.
    """
    sample = Sample(ledgers=len(paths))
    wanted = normalize_origin(origin) if origin else None
    seen: set[tuple[str, str]] = set()
    for path in paths:
        ledger = Ledger(path)
        origins = ledger.run_origins() if wanted else {}
        for row in ledger.prediction_training_rows():
            run_origin = origins.get(row["run_id"])
            if wanted and run_origin and normalize_origin(run_origin) != wanted:
                sample.out_of_scope += 1
                continue
            key = (row["run_id"], row["story_id"])
            if key in seen:
                sample.duplicates += 1
                continue
            seen.add(key)
            sample.rows.append(row)
    return sample


@dataclass(frozen=True)
class Calibration:
    """The proposed bars and the figures they were derived from."""

    sample_size: int
    p75_tokens: int
    rework_rate: float
    rework_threshold: float

    def proposed_yaml(self) -> str:
        return (
            f"{ROUTING_KEY}:\n"
            f"  predicted_tokens_threshold: {self.p75_tokens}\n"
            f"  rework_threshold: {self.rework_threshold}\n"
        )


def calibrate(sample: Sample) -> Calibration | None:
    """The bars for ``sample``, or ``None`` below :data:`MIN_GLOBAL_SAMPLE` stories."""
    rows = sample.rows
    if len(rows) < MIN_GLOBAL_SAMPLE:
        return None
    rework_rate = sum(1 for r in rows if r["actual_rework"]) / len(rows)
    threshold = min(
        MAX_REWORK_THRESHOLD,
        max(MIN_REWORK_THRESHOLD, round(rework_rate * REWORK_MULTIPLE, 2)),
    )
    return Calibration(
        sample_size=len(rows),
        p75_tokens=round(percentile([r["actual_tokens"] for r in rows], TOKENS_QUANTILE)),
        rework_rate=rework_rate,
        rework_threshold=threshold,
    )


def write_thresholds(
    target: Path, result: Calibration, *, ledgers: Sequence[Path], today: date
) -> None:
    """Set the two bars under ``model_routing:`` in ``target``, keeping the rest.

    Edited as text, not re-dumped, so the file's comments survive. A previous
    calibration's bars and dated comment are replaced rather than stacked.
    """
    comment = (
        f"  # calibrated {today.isoformat()} from {result.sample_size} reconciled "
        f"stories in: {', '.join(str(p) for p in ledgers)}"
    )
    block = [
        comment,
        f"  predicted_tokens_threshold: {result.p75_tokens}",
        f"  rework_threshold: {result.rework_threshold}",
    ]
    if not target.exists():
        target.write_text("\n".join([f"{ROUTING_KEY}:", *block]) + "\n", encoding="utf-8")
        return
    lines = target.read_text(encoding="utf-8").splitlines()
    header = next((i for i, l in enumerate(lines) if l.rstrip() == f"{ROUTING_KEY}:"), None)
    if header is None:
        raise ValueError(
            f"{target.name} has no block-style top-level '{ROUTING_KEY}:' to update"
        )
    stale = re.compile(rf"^\s+({'|'.join(_BARS)}):|^\s+# calibrated ")
    kept = [l for i, l in enumerate(lines) if i <= header or not stale.match(l)]
    kept[header + 1:header + 1] = block
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")

