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

# The single top-level key a per-repo override file uses.
ROUTING_KEY = "model_routing"

# The additive per-repo override file a consumer repo may ship at its root,
# mirroring `risk_gate.py`'s `.sdlc-risk-config.yaml` convention.
OVERRIDE_FILENAME = ".sdlc-model-routing.yaml"

# Profile names that mean "routing disabled — keep today's CLI-default behaviour".
_OFF_NAMES = {"", "off", "none"}


@dataclass(frozen=True)
class ModelRoutingConfig:
    """A resolved per-stage routing map plus its escalation policy.

    ``stage_models`` maps a pipeline stage name to the model alias it runs on by
    default. ``escalatable_stages`` are the stages that escalate to
    ``escalation_model`` when a story is high-risk or large (points ≥
    ``points_threshold``). ``pinned_stages`` always run on ``escalation_model``
    regardless of profile or signal — the adversarial skeptic is never cheapened
    (Story 14.2-001). The dataclass is frozen so a shared profile constant can be
    handed around without any caller mutating it; overrides build a new instance
    via :func:`dataclasses.replace`.
    """

    profile: str
    stage_models: dict[str, str]
    points_threshold: int
    escalation_model: str = OPUS
    escalatable_stages: frozenset[str] = frozenset({"build", "review"})
    pinned_stages: frozenset[str] = frozenset({"adversarial"})


# --- Built-in profiles ------------------------------------------------------
#
# Balanced is the shipped default: each stage on the cheapest tier that holds
# quality, with build/review escalating to Opus on a high-risk or large story
# and the adversarial skeptic pinned to Opus. Quality-first and Quota-max are the
# documented alternatives (all-Opus, and cheapest-everywhere-but-pinned-skeptic).

BALANCED = ModelRoutingConfig(
    profile="balanced",
    stage_models={
        "discovery": HAIKU,   # structured extraction (parse epics → queue)
        "build": SONNET,      # → Opus on high-risk / large (escalatable)
        "coverage": SONNET,   # tests need correctness
        "review": SONNET,     # → Opus on high-risk (escalatable)
        "adversarial": OPUS,  # pinned — never downgraded
        "merge": HAIKU,       # mechanical: fetch/resolve/PR/merge
        "bugfix": SONNET,     # base tier; per-attempt escalation is Story 14.2-003
        "reask": HAIKU,       # cheap envelope-only re-ask
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
        "adversarial": OPUS,  # still pinned — the skeptic is never cheapened
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

    A blank / ``off`` / ``none`` name (case-insensitive) disables routing so the
    run keeps today's CLI-default behaviour. An unknown name is a hard error
    rather than a silent fallback — a typo must never quietly change which model
    a high-stakes stage runs on.
    """
    name = (profile or "").strip().lower()
    if name in _OFF_NAMES:
        return None
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

    A pinned stage (adversarial) always returns ``escalation_model`` regardless
    of profile or signal. An escalatable stage (build/review) returns
    ``escalation_model`` when the story is high-risk or large (``points ≥
    points_threshold``); otherwise its mapped default tier.
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
        rebased = routing_config(section["profile"])
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
