# ABOUTME: Centralized degradation matrix + safe fallbacks for capability gaps (Story 20.5-002).
# ABOUTME: One testable decision point: parallel→serial, usage "unavailable", rate-limit backoff skipped.

from __future__ import annotations

from collections.abc import Mapping, Sequence
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

# Pipeline roles that can invoke *mutating* `gh`/`glab` operations against the
# code host — the roles whose agent holds real host credentials. `merge` is
# mandatory (it lands the change request); `review` is included by default
# because it posts reviews. Issue #654: these are the roles the deny baseline
# (`sdlc.dispatch.DENY_BASELINE`) actually protects, so routing one of them to a
# harness that never receives that baseline drops the secret/egress floor
# silently. Deliberately a small config constant, not a per-harness declaration:
# what a role *does with credentials* is a property of the pipeline, not of the
# CLI running it. Non-host-auth roles (build, coverage, docs) are unaffected.
HOST_AUTH_ROLES: tuple[str, ...] = ("merge", "review")

# The capability flag a harness declares when its dispatched command carries the
# deny baseline. Undeclared resolves to ``False`` via the usual conservative
# default, so a harness only earns the floor by claiming it.
DENY_BASELINE_CAPABILITY = "deny_baseline"


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
    # Issue #654: a host-auth role (merge/review) is routed to a harness that
    # renders no deny baseline. Unlike every other entry here this is NOT a safe
    # fallback — there is nothing to fall back *to*, because the controller
    # cannot impose a permission floor on a CLI it does not build the argv for.
    # It is therefore marked ``refusal`` and the run refuses to start rather than
    # degrading silently.
    UNDENIED_HOST_AUTH = "undenied_host_auth"


@dataclass(frozen=True)
class Degradation:
    """One applied fallback: what was downgraded and a human-readable reason.

    ``missing`` names the capability flag(s) whose absence triggered the
    fallback, so the record is self-explaining in the ledger.
    """

    kind: DegradationKind
    message: str
    missing: tuple[str, ...] = ()
    # Issue #654: True when this entry is a *refusal*, not a downgrade — the
    # controller has no safe alternative to fall back to, so the run must not
    # start. The degradation matrix stays the single decision point either way;
    # callers gate on this flag instead of re-deriving the rule.
    refusal: bool = False
    # The pipeline role(s) the entry applies to, when it is role-scoped (a
    # refusal names the host-auth role that triggered it). Empty for the
    # harness-wide fallbacks.
    roles: tuple[str, ...] = ()


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

    @property
    def refusals(self) -> tuple[Degradation, ...]:
        """Entries that are refusals rather than downgrades (issue #654).

        Non-empty means the matrix has no safe alternative to offer and the run
        must not start — the caller refuses instead of degrading.
        """
        return tuple(d for d in self.degradations if d.refusal)

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
                "refusal": d.refusal,
                "roles": list(d.roles),
            }
            for d in self.degradations
        ]


def evaluate_degradations(
    harness: str,
    capabilities: Mapping[str, bool],
    *,
    requested_mode: str = MODE_SERIAL,
    roles: Sequence[str] = (),
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

    ``roles`` (issue #654) names the pipeline roles this harness will actually
    run. When it contains a :data:`HOST_AUTH_ROLES` member and the harness does
    not declare :data:`DENY_BASELINE_CAPABILITY`, the plan carries an
    :attr:`DegradationKind.UNDENIED_HOST_AUTH` entry flagged ``refusal=True`` —
    the one matrix outcome that is *not* a silent downgrade, because there is no
    safe alternative: the controller cannot impose a permission floor on a CLI
    whose argv it does not build. Callers gate on :attr:`DegradationPlan.refusals`.
    Passing no ``roles`` (the default) never yields a refusal, so every existing
    call site is unchanged.

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

    host_auth = tuple(
        role for role in HOST_AUTH_ROLES if role in set(roles)
    )
    if host_auth and not capabilities.get(DENY_BASELINE_CAPABILITY):
        degradations.append(
            Degradation(
                kind=DegradationKind.UNDENIED_HOST_AUTH,
                message=(
                    f"harness {harness!r} renders no deny baseline "
                    f"(missing capability: {DENY_BASELINE_CAPABILITY}), so the "
                    f"host-auth role(s) {', '.join(host_auth)} would run with "
                    f"host credentials and no secret/egress floor"
                ),
                missing=(DENY_BASELINE_CAPABILITY,),
                refusal=True,
                roles=host_auth,
            )
        )

    return DegradationPlan(
        harness=harness,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        degradations=tuple(degradations),
    )
