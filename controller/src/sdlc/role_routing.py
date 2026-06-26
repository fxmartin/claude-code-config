# ABOUTME: Per-role harness routing — map each pipeline role to a harness (Story 20.2-001).
# ABOUTME: Parses the role→harness map, resolves each role from the registry, fails fast in preflight.

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from sdlc.harness import (
    HarnessConfig,
    HarnessError,
    resolve_harness,
)

# The pipeline roles the controller can route to a harness. These are the
# controller's dispatch stages (build, coverage/qa, review, merge) plus docs —
# the same stages model-routing already recognises (build._ROUTABLE_STAGES),
# named at the role granularity FX selects on the command line.
PIPELINE_ROLES: tuple[str, ...] = ("build", "coverage", "review", "merge", "docs")

# `qa` is an accepted spelling of the coverage role (the coverage agent runs the
# QA/coverage gate), so `--harness qa=codex` and `coverage=codex` are the same
# assignment. Keeping it an alias — rather than a separate role — is what lets the
# `review` and `qa` roles in the epic's headline example resolve without a third
# stage the controller does not actually dispatch.
ROLE_ALIASES: dict[str, str] = {"qa": "coverage"}


class RoleRoutingError(Exception):
    """A role→harness map was malformed, named an unknown role, or routed a role
    to an unknown/disabled harness. Raised so preflight fails fast (no half-run)."""


def canonical_role(role: str) -> str:
    """Normalise a role token to its canonical pipeline role, applying aliases.

    Case- and whitespace-insensitive. Raises :class:`RoleRoutingError` for an
    unknown role so a typo fails the parse rather than silently routing nothing.
    """
    token = role.strip().lower()
    token = ROLE_ALIASES.get(token, token)
    if token not in PIPELINE_ROLES:
        known = ", ".join(PIPELINE_ROLES)
        aliases = ", ".join(f"{a}->{c}" for a, c in ROLE_ALIASES.items())
        raise RoleRoutingError(
            f"unknown pipeline role {role!r}; known roles: {known} (aliases: {aliases})"
        )
    return token


def parse_role_harness_map(spec: str) -> dict[str, str]:
    """Parse a ``role=harness,role=harness`` spec into a ``{role: harness}`` map.

    ``spec`` is the value of ``sdlc build --harness=``, e.g.
    ``build=claude,review=codex,qa=codex``. Whitespace around tokens is ignored
    and role names are canonicalised (``qa`` -> ``coverage``). Empty segments are
    skipped, so a trailing comma is harmless. Raises :class:`RoleRoutingError` on
    a malformed entry, an unknown role, an empty harness name, or a role assigned
    two different harnesses (e.g. ``coverage=claude,qa=codex``).
    """
    role_map: dict[str, str] = {}
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise RoleRoutingError(
                f"malformed --harness entry {entry!r}; expected role=harness"
            )
        role_token, _, harness = entry.partition("=")
        harness = harness.strip()
        if not harness:
            raise RoleRoutingError(
                f"--harness entry {entry!r} is missing a harness name"
            )
        role = canonical_role(role_token)
        if role in role_map and role_map[role] != harness:
            raise RoleRoutingError(
                f"conflicting harness assignments for role {role!r}: "
                f"{role_map[role]!r} and {harness!r}"
            )
        role_map[role] = harness
    return role_map


def resolve_role_routing(
    role_map: Mapping[str, str] | None,
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, HarnessConfig]:
    """Resolve every pipeline role to the :class:`HarnessConfig` that will run it.

    Roles absent from ``role_map`` collapse to the default harness — today's
    behaviour, the built-in ``claude`` resolved through the existing dispatch seam
    (AC2). A role mapped to a non-default harness is resolved from the registry at
    ``config_path``. An unknown harness, a missing registry, or a **disabled**
    harness raises :class:`RoleRoutingError` so preflight fails fast before any
    stage runs (AC3 — no half-run).

    The returned mapping is keyed by every canonical role in
    :data:`PIPELINE_ROLES`, so a caller can look up the harness for any stage
    without re-consulting the raw map.
    """
    mapping = {canonical_role(r): h for r, h in (role_map or {}).items()}
    resolved: dict[str, HarnessConfig] = {}
    for role in PIPELINE_ROLES:
        name = mapping.get(role)
        try:
            harness = resolve_harness(name, config_path=config_path, env=env)
        except HarnessError as exc:
            raise RoleRoutingError(f"role {role!r} -> {exc}") from exc
        if not harness.enabled:
            raise RoleRoutingError(
                f"role {role!r} is routed to disabled harness {harness.name!r}; "
                "enable it in the registry or route the role to another harness"
            )
        resolved[role] = harness
    return resolved


def check_review_bridge(
    resolved: Mapping[str, HarnessConfig],
    *,
    reviewers_path: str | Path | None,
) -> None:
    """Reconcile the ``review`` role with the adversarial-reviewers registry.

    Technical-notes coordination point (Epic-08 owns reviewer consensus): the
    ``review`` role and the reviewer registry must *agree rather than conflict*.
    When ``review`` is routed to a harness that also appears as a reviewer in
    ``adversarial-reviewers.yaml``, that reviewer must be **enabled** there —
    otherwise the run would dispatch review to a harness the reviewer registry has
    switched off. A review harness *absent* from the reviewer registry is fine (it
    simply is not a consensus reviewer). A missing/unreadable reviewers file is a
    no-op: a malformed reviewer registry is Epic-08's gate to flag, not ours.
    """
    review = resolved.get("review")
    if review is None or reviewers_path is None:
        return
    path = Path(reviewers_path)
    if not path.exists():
        return
    # Local import keeps the adversarial module out of the hot import path and
    # avoids coupling routing to Epic-08's loader at module load time.
    from sdlc.adversarial import AdversarialError, load_reviewers_config

    try:
        _consensus, reviewers = load_reviewers_config(path)
    except AdversarialError:
        return
    for reviewer in reviewers:
        if reviewer.name == review.name and not reviewer.enabled:
            raise RoleRoutingError(
                f"review role is routed to harness {review.name!r}, but that "
                "reviewer is disabled in adversarial-reviewers.yaml — enable it "
                "there or route review to a different harness"
            )


def _config_file(name: str) -> Path | None:
    """Locate a checked-in controller config file, or ``None`` when absent.

    Resolved relative to this package's source tree (``controller/config/<name>``).
    Returns ``None`` when the file is missing — e.g. an installed wheel without the
    repo's config dir — in which case the default-harness path needs no registry
    and non-default routing fails fast with a clear message.
    """
    candidate = Path(__file__).resolve().parents[2] / "config" / name
    return candidate if candidate.exists() else None


def default_registry_path() -> Path | None:
    """The repo's harness registry (``controller/config/harnesses.yaml``), or None."""
    return _config_file("harnesses.yaml")


def default_reviewers_path() -> Path | None:
    """The repo's reviewer registry (``controller/config/adversarial-reviewers.yaml``)."""
    return _config_file("adversarial-reviewers.yaml")
