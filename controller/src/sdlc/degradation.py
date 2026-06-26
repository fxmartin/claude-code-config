# ABOUTME: Centralized degradation matrix + safe fallbacks for capability gaps (Story 20.5-002).
# ABOUTME: One testable decision point: parallel→serial, usage "unavailable", rate-limit backoff skipped.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sdlc.capability import MODE_PARALLEL, MODE_SERIAL

# Capabilities a parallel cohort requires: it fans a cohort across workers
# (``parallel``) each in its own git worktree (``worktree_isolation``). A harness
# missing either cannot run parallel safely and is degraded to serial — the safe
# alternative. This is the single canonical definition; ``capability.py``'s
# preflight reuses this module's decision rather than duplicating the rule.
PARALLEL_REQUIRES: tuple[str, ...] = ("parallel", "worktree_isolation")


class DegradationKind(str, Enum):
    """The kinds of safe fallback the controller applies when a harness lacks a
    capability. The *values* are persisted to the ledger, so they are stable
    strings — never renamed without a migration."""

    # A parallel cohort was requested but the harness can't isolate workers, so
    # it runs serially (AC1).
    PARALLEL_TO_SERIAL = "parallel_to_serial"
    # The harness reports no token usage / cost, so usage is recorded as
    # "unavailable" rather than fabricated as zero (AC2).
    USAGE_UNAVAILABLE = "usage_unavailable"
    # The harness has no 429 / reset semantics, so rate-limit backoff is skipped
    # — no fabricated rate-limit handling (AC2).
    RATE_LIMIT_SKIPPED = "rate_limit_skipped"


@dataclass(frozen=True)
class Degradation:
    """One applied fallback: what was downgraded and a human-readable reason.

    ``missing`` names the capability flag(s) whose absence triggered the
    fallback, so the record is self-explaining in the ledger.
    """

    kind: DegradationKind
    message: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class DegradationPlan:
    """The full set of fallbacks for one harness under a requested run mode.

    ``effective_mode`` is the mode the run should actually use: it equals
    ``requested_mode`` unless a capability gap forced a downgrade (then it is the
    safe alternative, ``serial``). ``degradations`` is every fallback applied,
    each recordable in the ledger so nothing degrades silently (AC3).
    """

    harness: str
    requested_mode: str
    effective_mode: str
    degradations: tuple[Degradation, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        """True when any fallback was applied."""
        return bool(self.degradations)

    @property
    def mode_degraded(self) -> bool:
        """True when the run mode itself was downgraded (e.g. parallel→serial)."""
        return self.effective_mode != self.requested_mode

    def has(self, kind: DegradationKind) -> bool:
        """Whether a specific fallback was applied."""
        return any(d.kind is kind for d in self.degradations)

    def kinds(self) -> frozenset[DegradationKind]:
        """The set of fallback kinds applied."""
        return frozenset(d.kind for d in self.degradations)

    def log_lines(self) -> list[str]:
        """One human-readable line per degradation for stderr / the event log."""
        return [d.message for d in self.degradations]

    def to_records(self) -> list[dict[str, Any]]:
        """Structured rows — one per degradation — for the ledger / run summary."""
        return [
            {
                "harness": self.harness,
                "kind": d.kind.value,
                "missing": list(d.missing),
                "message": d.message,
                "requested_mode": self.requested_mode,
                "effective_mode": self.effective_mode,
            }
            for d in self.degradations
        ]


def evaluate_degradations(
    harness: str,
    capabilities: Mapping[str, bool],
    *,
    requested_mode: str = MODE_SERIAL,
) -> DegradationPlan:
    """Resolve every safe fallback for ``harness`` under ``requested_mode``.

    This is the single, testable decision point the rest of the controller gates
    on (Story 20.5-002). ``capabilities`` is the resolved capability map (see
    :func:`sdlc.capability.resolve_capabilities`, where an undeclared flag is
    ``False``). The three fallbacks:

    - **parallel→serial** (AC1): a ``parallel`` request on a harness missing
      ``parallel`` or ``worktree_isolation`` downgrades to ``serial`` so the
      cohort never crashes mid-run.
    - **usage unavailable** (AC2): a harness without ``usage_tracking`` has its
      cost/usage recorded as "unavailable" rather than fabricated as zero.
    - **rate-limit skipped** (AC2): a harness without ``rate_limit_aware`` skips
      rate-limit backoff — no fabricated 429 handling.

    A fully capable harness (e.g. the built-in Claude harness) yields an empty
    plan, so wiring this in is purely additive for the default path.
    """
    degradations: list[Degradation] = []

    effective_mode = requested_mode
    if requested_mode == MODE_PARALLEL:
        missing = tuple(
            cap for cap in PARALLEL_REQUIRES if not capabilities.get(cap)
        )
        if missing:
            effective_mode = MODE_SERIAL
            degradations.append(
                Degradation(
                    kind=DegradationKind.PARALLEL_TO_SERIAL,
                    message=(
                        f"harness {harness!r} cannot run mode=parallel "
                        f"(missing capability: {', '.join(missing)}); "
                        f"degrading to mode=serial"
                    ),
                    missing=missing,
                )
            )

    if not capabilities.get("usage_tracking"):
        degradations.append(
            Degradation(
                kind=DegradationKind.USAGE_UNAVAILABLE,
                message=(
                    f"harness {harness!r} has no usage tracking; "
                    f"cost/usage recorded as unavailable"
                ),
                missing=("usage_tracking",),
            )
        )

    if not capabilities.get("rate_limit_aware"):
        degradations.append(
            Degradation(
                kind=DegradationKind.RATE_LIMIT_SKIPPED,
                message=(
                    f"harness {harness!r} has no rate-limit semantics; "
                    f"rate-limit backoff skipped (no fabricated 429 handling)"
                ),
                missing=("rate_limit_aware",),
            )
        )

    return DegradationPlan(
        harness=harness,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        degradations=tuple(degradations),
    )
