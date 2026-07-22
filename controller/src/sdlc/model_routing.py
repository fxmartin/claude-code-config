# ABOUTME: Per-task model routing (Story 14.2-001) — pick a model per pipeline stage.
# ABOUTME: Ships a Balanced default map with risk/points escalation and a pinned Opus skeptic.

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

# Model tier aliases. These are the values handed to `claude --model`; the CLI
# accepts the short aliases (`haiku`/`sonnet`/`opus`) as well as full model ids,
# so a per-repo override can pin a precise id (e.g. `claude-opus-4-8`) without
# any code change. They are deliberately plain strings — the routing layer never
# interprets them, it only chooses which one a stage gets.
HAIKU = "haiku"
SONNET = "sonnet"
OPUS = "opus"

# The tier ladder, cheapest → strongest. Cheap-first retry escalation
# (Story 14.2-003) walks up this and caps at the top tier, so a stuck stage
# climbs Haiku→Sonnet→Opus instead of retrying on the model that just failed.
TIER_LADDER: tuple[str, ...] = (HAIKU, SONNET, OPUS)

# The single top-level key a per-repo override file uses.
ROUTING_KEY = "model_routing"

# The additive per-repo override file a consumer repo may ship at its root,
# mirroring `risk_gate.py`'s `.sdlc-risk-config.yaml` convention.
OVERRIDE_FILENAME = ".sdlc-model-routing.yaml"

# Profile names that mean "routing disabled — every stage on the CLI default".
# Story 28.4-001 removed the empty string from this set: an *absent* profile is
# no longer an opt-out, it is the unset state that resolves to the default map
# below. Only an explicit `off` / `none` disables routing, so a cost-governance
# control can no longer fail silent-and-expensive.
_OFF_NAMES = {"off", "none"}

# The canonical explicit opt-out value, persisted on a run row whose routing is
# off so "off" is a stated fact rather than an absent key.
OFF = "off"

# The profile an unset `model_profile` resolves to (Story 28.4-001). Balanced is
# the effective default: cheap tiers for mechanical stages, Sonnet for the ones
# that need correctness, Opus only where a high-risk / large story earns it.
DEFAULT_PROFILE = "balanced"


@dataclass(frozen=True)
class ModelRoutingConfig:
    """A resolved per-stage routing map plus its escalation policy.

    ``stage_models`` maps a pipeline stage name to the model alias it runs on by
    default. ``escalatable_stages`` are the stages that escalate to
    ``escalation_model`` when a story is high-risk or large (points ≥
    ``points_threshold``). ``pinned_stages`` always run on ``escalation_model``
    regardless of profile or signal (Story 14.2-001) — a still-supported
    mechanism, though no shipped profile pins a stage after Story 27.2-002 moved
    the adversarial skeptic from pinned to escalatable: its Opus is now a *floor*
    paid only on high-risk / large stories, so a low-risk story tiers it down.
    The dataclass is frozen so a shared profile constant can be handed around
    without any caller mutating it; overrides build a new instance via
    :func:`dataclasses.replace`.
    """

    profile: str
    stage_models: dict[str, str]
    points_threshold: int
    escalation_model: str = OPUS
    escalatable_stages: frozenset[str] = frozenset({"build", "review", "adversarial"})
    pinned_stages: frozenset[str] = frozenset()


# --- Built-in profiles ------------------------------------------------------
#
# Balanced is the shipped default: each stage on the cheapest tier that holds
# quality, with build/review/adversarial escalating to Opus on a high-risk or
# large story (the adversarial skeptic's Opus is a *floor*, not a pin — Story
# 27.2-002 — so a low-risk story runs it on Sonnet). Quality-first and Quota-max
# are the documented alternatives (all-Opus, and cheapest-everywhere).

BALANCED = ModelRoutingConfig(
    profile="balanced",
    stage_models={
        "discovery": HAIKU,     # structured extraction (parse epics → queue)
        "build": SONNET,        # → Opus on high-risk / large (escalatable)
        "coverage": SONNET,     # tests need correctness
        "review": SONNET,       # → Opus on high-risk (escalatable)
        "adversarial": SONNET,  # → Opus floor on high-risk / large (27.2-002)
        "merge": HAIKU,         # mechanical: fetch/resolve/PR/merge
        "bugfix": SONNET,       # base tier; per-attempt escalation is Story 14.2-003
        "reask": HAIKU,         # cheap envelope-only re-ask
    },
    points_threshold=8,
)

QUALITY_FIRST = ModelRoutingConfig(
    profile="quality-first",
    stage_models={
        "discovery": OPUS,
        "build": OPUS,
        "coverage": OPUS,
        "review": OPUS,
        "adversarial": OPUS,
        "merge": OPUS,
        "bugfix": OPUS,
        "reask": OPUS,
    },
    points_threshold=1,  # always already at the top tier; escalation is a no-op
)

QUOTA_MAX = ModelRoutingConfig(
    profile="quota-max",
    stage_models={
        "discovery": HAIKU,
        "build": HAIKU,
        "coverage": HAIKU,
        "review": HAIKU,
        "adversarial": SONNET,  # → Opus floor on high-risk / large (27.2-002);
                                # never below Sonnet — the skeptic's cheap floor
        "merge": HAIKU,
        "bugfix": HAIKU,
        "reask": HAIKU,
    },
    # Escalate only the genuinely large/high-stakes build/review so the cheap
    # path stays the common path; a higher bar than Balanced.
    points_threshold=13,
)

_PROFILES: dict[str, ModelRoutingConfig] = {
    BALANCED.profile: BALANCED,
    QUALITY_FIRST.profile: QUALITY_FIRST,
    QUOTA_MAX.profile: QUOTA_MAX,
}


def routing_config(profile: str | None) -> ModelRoutingConfig | None:
    """Resolve a built-in profile by name, or ``None`` when routing is off.

    An **unset** profile (``None`` / blank) resolves to :data:`DEFAULT_PROFILE`
    — Balanced — because routing that is off by default is a cost control that
    fails silent-and-expensive: under the Story 14.2-001 semantics every run in
    the 2026-07-19 dataset (374 stage attempts) ran the CLI default of the day,
    with zero Sonnet sessions, while the operator believed routing was on
    (Story 28.4-001).

    Only an explicit ``off`` / ``none`` (case-insensitive) disables routing, in
    which case every stage falls back to the CLI default model. An unknown name
    is a hard error rather than a silent fallback — a typo must never quietly
    change which model a high-stakes stage runs on.
    """
    name = (profile or "").strip().lower()
    if name in _OFF_NAMES:
        return None
    if not name:
        name = DEFAULT_PROFILE
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown model-routing profile: {profile!r} "
            f"(expected one of {sorted(_PROFILES)} or off)"
        ) from None


def select_model(
    stage: str,
    config: ModelRoutingConfig | None,
    *,
    points: int = 0,
    high_risk: bool = False,
) -> str | None:
    """Choose the model for ``stage`` under ``config``, or ``None`` for CLI-default.

    Returns ``None`` when routing is off (``config is None``) or the stage is not
    in the map — in both cases the dispatcher adds no ``--model`` and the CLI
    default (Opus today) stands, so behaviour is unchanged (Story 14.2-001 AC).

    A pinned stage always returns ``escalation_model`` regardless of profile or
    signal (no shipped profile pins a stage; the mechanism stays for overrides /
    future profiles). An escalatable stage (build/review/adversarial) returns
    ``escalation_model`` when the story is high-risk or large (``points ≥
    points_threshold``); otherwise its mapped default tier. The adversarial
    skeptic is escalatable so its Opus is a *floor* — paid on high-risk / large
    stories, tiered down to its cheap base (Sonnet) for a low-risk story (Story
    27.2-002); tiering can never downgrade a high-risk story below the floor.
    """
    if config is None:
        return None
    if stage in config.pinned_stages:
        return config.escalation_model
    base = config.stage_models.get(stage)
    if base is None:
        return None
    if stage in config.escalatable_stages and (
        high_risk or points >= config.points_threshold
    ):
        return config.escalation_model
    return base


def escalate_model(base: str | None, steps: int) -> str | None:
    """Bump ``base`` up the tier ladder by ``steps``, capped at the strongest tier.

    The cheap-first lever (Story 14.2-003): a stage that fails into the bugfix
    loop is retried one tier stronger per attempt, rather than re-running on the
    model that just failed — so Opus is paid for only when a stage is actually
    stuck, not on the common passing path.

    Returns ``base`` unchanged when:

    * ``steps <= 0`` — the first-pass / cheap path (no escalation, AC2);
    * ``base is None`` — routing is off, so the CLI default stands and there is
      no tier to climb;
    * ``base`` is not a known ladder tier — a custom / pinned model id the router
      cannot reason about is never silently rewritten.

    At or above the top tier the bump is a no-op (AC3: escalating an already-Opus
    stage does nothing).
    """
    if base is None or steps <= 0:
        return base
    try:
        idx = TIER_LADDER.index(base)
    except ValueError:
        return base
    return TIER_LADDER[min(idx + steps, len(TIER_LADDER) - 1)]


def _coerce_override(base: ModelRoutingConfig, raw: Any) -> ModelRoutingConfig:
    """Apply an additive override mapping onto ``base``, returning a new config.

    Recognised keys under ``model_routing``:

    * ``profile`` — re-select the base built-in profile (applied first);
    * ``stages`` — a mapping of stage → model alias, merged over the base map;
    * ``points_threshold`` — an int that replaces the escalation threshold;
    * ``escalation_model`` — the model a stage escalates / is pinned to.

    The override is additive: any stage it omits keeps the base profile's value,
    so a repo can tune a single stage without restating the whole map. A
    malformed shape raises :class:`ValueError`.
    """
    if not isinstance(raw, dict) or ROUTING_KEY not in raw:
        raise ValueError(f"override must define a top-level {ROUTING_KEY!r} key")
    section = raw[ROUTING_KEY]
    if not isinstance(section, dict):
        raise ValueError(f"{ROUTING_KEY!r} must be a mapping")

    cfg = base
    if "profile" in section:
        raw_profile = section["profile"]
        # YAML 1.1 parses a bare `off` as the boolean False, so normalise before
        # resolving. Since Story 28.4-001 a blank profile resolves to Balanced
        # rather than to None, so `rebased is None` alone no longer catches an
        # empty value — an override must always *name* a profile.
        if isinstance(raw_profile, bool):
            raw_profile = "off" if raw_profile is False else "on"
        if not str(raw_profile or "").strip():
            raise ValueError("override 'profile' must name a profile, not be blank")
        rebased = routing_config(raw_profile)
        if rebased is None:
            raise ValueError("override profile cannot be 'off' (omit the file instead)")
        cfg = rebased

    stage_models = dict(cfg.stage_models)
    stages = section.get("stages", {})
    if stages:
        if not isinstance(stages, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in stages.items()
        ):
            raise ValueError("'stages' must be a mapping of stage → model alias")
        stage_models.update(stages)

    threshold = cfg.points_threshold
    if "points_threshold" in section:
        raw_threshold = section["points_threshold"]
        if not isinstance(raw_threshold, int) or isinstance(raw_threshold, bool):
            raise ValueError("'points_threshold' must be an integer")
        threshold = raw_threshold

    escalation_model = cfg.escalation_model
    if "escalation_model" in section:
        if not isinstance(section["escalation_model"], str):
            raise ValueError("'escalation_model' must be a string")
        escalation_model = section["escalation_model"]

    return replace(
        cfg,
        stage_models=stage_models,
        points_threshold=threshold,
        escalation_model=escalation_model,
    )


def load_routing_config(
    profile: str | None,
    *,
    override_path: Path | None = None,
    override_text: str | None = None,
) -> ModelRoutingConfig | None:
    """Resolve the routing config for ``profile``, applying a per-repo override.

    Returns ``None`` when routing is off. The override (``override_text`` for
    tests, else the YAML at ``override_path`` when it exists) is *additive* on
    top of the selected built-in profile — a missing file is silently ignored,
    so the common path is just the chosen profile constant. A present-but-invalid
    override is a hard error rather than a silent fallback.
    """
    base = routing_config(profile)
    if base is None:
        return None

    text = override_text
    if text is None and override_path is not None and override_path.is_file():
        text = override_path.read_text(encoding="utf-8")
    if text is None:
        return base

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"model-routing override is not valid YAML: {exc}") from exc
    return _coerce_override(base, raw)


# ---------------------------------------------------------------------------
# Story 28.4-001: resolve-and-freeze — the run's routing snapshot
# ---------------------------------------------------------------------------
#
# The snapshot is the *fully-resolved* routing config: the profile name plus the
# effective per-stage map and escalation thresholds after every override. It is
# resolved once at run creation, persisted on the run row, and replayed verbatim
# by every resume — so an edit to `.sdlc-model-routing.yaml`, `SDLC_AGENT_CMD`,
# or a per-stage `--model` between a run and its resume can never alter that
# run's routing (the Epic-10 / Epic-12 "resume identically" contract).
#
# It is plain JSON-able data (never a dataclass) because it round-trips through
# a TEXT ledger column, and it is the same object the startup banner renders, so
# what is printed live and what is replayed on resume cannot drift.


def routing_snapshot(
    config: ModelRoutingConfig | None,
    *,
    overrides: dict[str, str] | None = None,
    agent_cmd: str | None = None,
) -> dict:
    """Freeze ``config`` (plus its overrides) into a JSON-able routing snapshot.

    ``config`` is the resolved profile (``None`` when routing is off);
    ``overrides`` are the explicit per-stage ``--model-<stage>`` pins, which win
    over the map and are therefore merged into ``stage_models`` so the snapshot
    states the *effective* model each stage starts on; ``agent_cmd`` records a
    ``SDLC_AGENT_CMD`` override so the banner can say the whole command was
    replaced. A routing-off snapshot keeps an empty map and an explicit ``off``
    profile — never an absent key, so "off" is always a stated fact.
    """
    overrides = dict(overrides or {})
    if config is None:
        return {
            "profile": OFF,
            "stage_models": {},
            "overrides": overrides,
            "agent_cmd": agent_cmd or "",
        }
    stage_models = dict(config.stage_models)
    stage_models.update(overrides)
    return {
        "profile": config.profile,
        "stage_models": stage_models,
        "points_threshold": config.points_threshold,
        "escalation_model": config.escalation_model,
        "escalatable_stages": sorted(config.escalatable_stages),
        "pinned_stages": sorted(config.pinned_stages),
        "overrides": overrides,
        "agent_cmd": agent_cmd or "",
    }


def is_routing_off(snapshot: dict | None) -> bool:
    """Is ``snapshot`` a routing-off snapshot (or an absent/legacy one)?

    An absent snapshot reads as off: only a run created after Story 28.4-001
    carries one, and a run that predates the default flip routed off, so the
    conservative reading is also the historically accurate one.
    """
    if not snapshot:
        return True
    return str(snapshot.get("profile") or "").strip().lower() in _OFF_NAMES


def config_from_snapshot(snapshot: dict | None) -> ModelRoutingConfig | None:
    """Rebuild the routing config a snapshot froze, or ``None`` when it is off.

    The inverse of :func:`routing_snapshot`, used by resume to replay a run's
    original routing *without* re-reading the profile table, the per-repo
    override file, or the overrides — the snapshot alone decides. Missing
    escalation fields fall back to the Balanced defaults so a hand-written or
    partially-populated snapshot still yields a usable config rather than
    raising mid-run.
    """
    if is_routing_off(snapshot):
        return None
    assert snapshot is not None  # narrowed by is_routing_off
    stage_models = {
        str(k): str(v) for k, v in (snapshot.get("stage_models") or {}).items()
    }
    escalatable = snapshot.get("escalatable_stages")
    pinned = snapshot.get("pinned_stages")
    return ModelRoutingConfig(
        profile=str(snapshot.get("profile")),
        stage_models=stage_models,
        points_threshold=int(snapshot.get("points_threshold", BALANCED.points_threshold)),
        escalation_model=str(snapshot.get("escalation_model") or OPUS),
        escalatable_stages=(
            frozenset(escalatable) if escalatable is not None
            else BALANCED.escalatable_stages
        ),
        pinned_stages=frozenset(pinned or ()),
    )


def routing_banner(snapshot: dict) -> list[str]:
    """Render the run-start routing banner for ``snapshot`` (one string per line).

    Names the resolved profile, the effective per-stage map after overrides, and
    the escalation thresholds in effect — the three things that decide what a run
    spends. The off state prints **loudly** (an upper-case ``MODEL ROUTING OFF``
    line) because it is the expensive state: every stage falls back to the CLI
    default, which is whatever the CLI ships that day.
    """
    agent_cmd = str(snapshot.get("agent_cmd") or "")
    if is_routing_off(snapshot):
        lines = [
            "MODEL ROUTING OFF: CLI default model used for ALL stages "
            "(set --model-routing=balanced, or model_profile, to engage routing)"
        ]
    else:
        stage_models = snapshot.get("stage_models") or {}
        mapping = " ".join(f"{s}={m}" for s, m in sorted(stage_models.items()))
        lines = [
            f"model routing: profile={snapshot.get('profile')}",
            f"model routing: map {mapping}",
            "model routing: escalation → "
            f"{snapshot.get('escalation_model')} on high-risk or points >= "
            f"{snapshot.get('points_threshold')} "
            f"(stages: {','.join(snapshot.get('escalatable_stages') or [])})",
        ]
        pinned = snapshot.get("pinned_stages") or []
        if pinned:
            lines.append(
                f"model routing: pinned to {snapshot.get('escalation_model')}: "
                f"{','.join(pinned)}"
            )
    overrides = snapshot.get("overrides") or {}
    if overrides:
        pins = " ".join(f"{s}={m}" for s, m in sorted(overrides.items()))
        lines.append(f"model routing: explicit --model overrides win: {pins}")
    if agent_cmd:
        lines.append(
            f"model routing: SDLC_AGENT_CMD overrides the whole agent command "
            f"({agent_cmd}) — its own model selection wins"
        )
    return lines
