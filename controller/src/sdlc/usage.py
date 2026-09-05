# ABOUTME: Honest token accounting across harnesses (Story 31.2-002) — the four usage
# ABOUTME: components carried individually, with provenance, so no total reads as like-for-like.

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The four components every honest token figure is made of, in the order every
# surface reports them. A *total* is a lossy summary of these: 15,160 tokens of
# cache-creation and 14,152 tokens of cache-read look like parity and are nothing
# of the kind, which is why the components — not the total — are what travels.
USAGE_COMPONENTS: tuple[str, ...] = (
    "input",
    "output",
    "cache_read",
    "cache_creation",
)

# component -> the agent envelope's key for it (mirrors build._RESULT_USAGE_KEYS
# and dispatch's envelope parsing), which map the same envelope to the ledger columns.
ENVELOPE_KEYS: dict[str, str] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}

# Provenance of a token figure. These strings are persisted (scoreboards,
# baselines), so they are stable — never renamed without a migration.
MEASURED = "measured"  # reported by a harness that declares usage_tracking
ESTIMATED = "estimated"  # the pre-dispatch estimate, the only figure available
EXTERNAL = "external"  # an approximate count from a serving layer (the AC7 seam)
MIXED = "mixed"  # an aggregate whose runs disagree on provenance
UNAVAILABLE = "unavailable"  # no figure at all — never 0

# Sources that must never be silently compared against a MEASURED figure: an
# estimate and a measurement of the same run are different quantities, and an
# aggregate of both is neither.
APPROXIMATE_SOURCES: frozenset[str] = frozenset({ESTIMATED, EXTERNAL, MIXED})

# How far two component mixes may drift before the arms stop describing the same
# work. Expressed as the largest per-component share gap: 0.25 means one arm may
# spend a quarter more of its budget on (say) cache-creation than the other and
# still be judged like-for-like. The field case is ~100 points apart.
DEFAULT_MIX_TOLERANCE = 0.25


@dataclass(frozen=True)
class TokenBreakdown:
    """One dispatch's token usage kept as its four components plus a provenance.

    ``None`` on a component means "not reported"; ``0`` means "reported as zero".
    Collapsing the two is the whole bug this story closes, so they never merge:
    :attr:`total` is ``None`` — not ``0`` — when nothing was reported at all.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    source: str = UNAVAILABLE

    @property
    def components(self) -> dict[str, int | None]:
        """The four components by canonical name, in :data:`USAGE_COMPONENTS` order."""
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_creation": self.cache_creation_tokens,
        }

    @property
    def available(self) -> bool:
        """Whether any component was reported at all."""
        return any(v is not None for v in self.components.values())

    @property
    def total(self) -> int | None:
        """Sum of the reported components, or ``None`` when none were reported."""
        if not self.available:
            return None
        return sum(v or 0 for v in self.components.values())

    @property
    def approximate(self) -> bool:
        """Whether this figure is an estimate/approximation rather than a measurement."""
        return self.source in APPROXIMATE_SOURCES

    @property
    def mix(self) -> dict[str, float] | None:
        """Each component's share of the total, or ``None`` when there is no mix.

        A zero total has no mix (nothing to apportion), and neither does an
        unavailable breakdown — in both cases the comparator has nothing to judge
        and must say so rather than assume parity.
        """
        total = self.total
        if not total:
            return None
        return {name: (value or 0) / total for name, value in self.components.items()}


# The single "no figure" value. Identity-comparable so a caller can assert on it.
UNAVAILABLE_USAGE = TokenBreakdown()


def breakdown_from_envelope(
    usage: Mapping[str, Any] | None, *, source: str = MEASURED
) -> TokenBreakdown:
    """Read an agent usage envelope into a :class:`TokenBreakdown`.

    An absent envelope, or one whose four component keys are all ``None``, yields
    :data:`UNAVAILABLE_USAGE` — the agent carried no usage, which is not zero
    usage. ``source`` labels the provenance for surfaces that must not present an
    estimate as a measurement.
    """
    if not usage:
        return UNAVAILABLE_USAGE
    values = {name: usage.get(key) for name, key in ENVELOPE_KEYS.items()}
    if all(v is None for v in values.values()):
        return UNAVAILABLE_USAGE
    return TokenBreakdown(
        input_tokens=_as_int(values["input"]),
        output_tokens=_as_int(values["output"]),
        cache_read_tokens=_as_int(values["cache_read"]),
        cache_creation_tokens=_as_int(values["cache_creation"]),
        source=source,
    )


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


# ---------------------------------------------------------------------------
# Capability gate + the external counter seam
# ---------------------------------------------------------------------------

# A token counter supplies an approximate breakdown for a harness that cannot
# report its own usage — a local model whose serving layer exposes prompt /
# completion counts, for instance. It receives whatever usage envelope the
# dispatch produced (often ``None``) and returns a breakdown or ``None`` to
# decline. This story commits to no particular server: it only leaves the seam,
# and whatever comes through it is labelled :data:`EXTERNAL`, never MEASURED.
TokenCounter = Callable[[Mapping[str, Any] | None], "TokenBreakdown | None"]

_COUNTERS: dict[str, TokenCounter] = {}


def register_token_counter(harness: str, counter: TokenCounter) -> None:
    """Register an external token counter for ``harness`` (the AC7 seam)."""
    _COUNTERS[harness] = counter


def unregister_token_counter(harness: str) -> None:
    """Drop ``harness``'s external token counter, if any."""
    _COUNTERS.pop(harness, None)


def external_breakdown(
    harness: str | None, usage: Mapping[str, Any] | None
) -> TokenBreakdown | None:
    """Ask ``harness``'s registered counter for an approximate breakdown.

    ``None`` when no counter is registered, the counter declines, or it returns
    something with no components. Whatever it does return is re-labelled
    :data:`EXTERNAL` so it can never pass as a measurement.
    """
    if harness is None:
        return None
    counter = _COUNTERS.get(harness)
    if counter is None:
        return None
    supplied = counter(usage)
    if supplied is None or not supplied.available:
        return None
    return TokenBreakdown(
        input_tokens=supplied.input_tokens,
        output_tokens=supplied.output_tokens,
        cache_read_tokens=supplied.cache_read_tokens,
        cache_creation_tokens=supplied.cache_creation_tokens,
        source=EXTERNAL,
    )


def usage_is_tracked(capabilities: Mapping[str, bool] | None) -> bool:
    """Whether a harness's capability map earns it a token figure.

    ``None`` means no capability map was resolved (the built-in dispatch seam),
    which keeps today's behaviour. Otherwise this is exactly ``capability.py``'s
    conservative default: a harness only earns the axis it explicitly claims, so
    an undeclared or ``false`` ``usage_tracking`` is absent — no per-harness
    special cases anywhere.
    """
    if capabilities is None:
        return True
    return bool(capabilities.get("usage_tracking"))


def harness_breakdown(
    usage: Mapping[str, Any] | None,
    *,
    capabilities: Mapping[str, bool] | None = None,
    harness: str | None = None,
    source: str = MEASURED,
) -> TokenBreakdown:
    """The breakdown to record for one dispatch, gated on the harness's capability.

    A harness that does not declare ``usage_tracking`` reports *unavailable* —
    never zero, and never the numbers it happened to print. A harness that grows
    usage telemetry later (Epic-29's 29.2-003 for OpenCode) flips its
    ``usage_tracking`` flag and every surface here starts trusting it, with no
    change to this machinery.

    When the harness cannot self-report but an external counter is registered for
    it (:func:`register_token_counter`), that counter's approximate figure is used
    and labelled :data:`EXTERNAL`.
    """
    if usage_is_tracked(capabilities):
        return breakdown_from_envelope(usage, source=source)
    external = external_breakdown(harness, usage)
    return external if external is not None else UNAVAILABLE_USAGE


# ---------------------------------------------------------------------------
# Mixes + aggregate provenance
# ---------------------------------------------------------------------------


def mix_divergence(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """The largest per-component share gap between two mixes (0.0 = identical)."""
    return max(abs(a.get(name, 0.0) - b.get(name, 0.0)) for name in USAGE_COMPONENTS)


def describe_mix(mix: Mapping[str, float] | None) -> str:
    """A one-phrase description of a mix: its dominant component and that share."""
    if not mix:
        return "unknown mix"
    name, share = max(mix.items(), key=lambda item: item[1])
    return f"{share * 100:.1f}% {name}"


def aggregate_source(breakdowns: Iterable[TokenBreakdown]) -> str:
    """Fold the provenance of several runs into one label for their aggregate.

    Unavailable runs are ignored (they contribute no figure); with none left the
    aggregate is :data:`UNAVAILABLE`. Runs that agree keep their source; runs that
    disagree are :data:`MIXED`, which is approximate — an aggregate of a
    measurement and an estimate is neither, and must not be compared as one.
    """
    sources = {b.source for b in breakdowns if b.available}
    if not sources:
        return UNAVAILABLE
    if len(sources) == 1:
        return next(iter(sources))
    return MIXED
