# ABOUTME: Config-driven harness registry — declares each agent harness and how to invoke it.
# ABOUTME: Story 20.1-001 — generalizes the dispatch seam + adversarial-reviewers registry.

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from sdlc.dispatch import DEFAULT_AGENT_CMD, resolve_agent_cmd

# The registry schema ships inside the package (alongside the Epic-07 agent
# schemas and the adversarial-reviewer schema) so it resolves under
# `uv tool install`, where the source tree is gone.
_SCHEMA_FILE = "harness-registry.schema.json"

# Name of the built-in default harness. When a role/run names none — and there is
# no `SDLC_AGENT_CMD` override — the controller dispatches to this harness via the
# existing dispatch seam, byte-identical to today's `DEFAULT_AGENT_CMD`.
DEFAULT_HARNESS = "claude"

# The parser id of the built-in Claude harness. Parsers are registered by id in
# Story 20.1-002; here it is metadata so the registry abstraction is complete.
_CLAUDE_PARSER = "claude-stream-json"

# Capability flags assumed for the built-in Claude harness (and an `SDLC_AGENT_CMD`
# override, which is Claude under the hood for FX's environment). A registry-defined
# harness declares its own flags in `harnesses.yaml`; these are the defaults for the
# slot that goes through the existing dispatch seam.
_BUILTIN_CAPABILITIES: dict[str, bool] = {
    "worktree_isolation": True,
    "parallel": True,
    "json_contract": True,
    "usage_tracking": True,
    "rate_limit_aware": True,
}


class HarnessError(Exception):
    """The harness registry was malformed, or an unknown harness was requested."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    resource = resources.files(__package__) / "schemas" / _SCHEMA_FILE
    return json.loads(resource.read_text(encoding="utf-8"))


# Exposed for tests and callers that want to introspect the published contract.
HARNESS_REGISTRY_SCHEMA: dict[str, Any] = _load_schema()


@dataclass(frozen=True)
class HarnessConfig:
    """One harness adapter — how to invoke it and what it can do.

    ``source`` records where the entry came from: ``registry`` (a
    ``harnesses.yaml`` entry), ``builtin`` (the default Claude harness), or
    ``env`` (an ``SDLC_AGENT_CMD`` override re-expressed as an ad-hoc entry). The
    ``builtin`` / ``env`` slots resolve their argv through the existing dispatch
    seam (:func:`sdlc.dispatch.resolve_agent_cmd`) so the default and override
    paths stay byte-identical to today; a ``registry`` entry renders its own
    command template.
    """

    name: str
    command: str
    parser: str
    flags: list[str] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    enabled: bool = True
    source: str = "registry"
    # Optional command that confirms the harness CLI is installed/authenticated.
    # Consumed by the capability preflight (Story 20.5-001); ``None`` means the
    # harness is not probed (status "unknown").
    probe: str | None = None

    def render_command(self, **placeholders: Any) -> list[str]:
        """Render this harness's command template into an argv, appending flags.

        Substitutes ``{pr_number}``/``{pr_url}``/``{story_id}`` placeholders the
        same way the reviewer registry does, then appends the invocation flags.
        """
        rendered = self.command.format(**placeholders) if placeholders else self.command
        return shlex.split(rendered) + list(self.flags)

    def to_argv(self, *, model: str | None = None) -> list[str]:
        """The command to launch an agent on this harness.

        The ``builtin`` and ``env`` slots delegate to
        :func:`sdlc.dispatch.resolve_agent_cmd` so the default-command and
        ``SDLC_AGENT_CMD`` paths are byte-identical to today (deny baseline +
        routed ``--model`` decoration included; the env escape hatch owns its own
        model, so ``model`` is deliberately ignored there). A ``registry`` entry
        renders its own template — it owns its invocation surface.
        """
        if self.source in ("builtin", "env"):
            return resolve_agent_cmd(model=model)
        return self.render_command()


def _format_error(error: Any) -> str:
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance or {}))
        field_name = missing[0] if missing else "?"
        return f"harness registry is missing required field {field_name!r}: {error.message}"
    location = "/".join(str(part) for part in error.absolute_path)
    where = f"field {location!r}" if location else "the registry root"
    return f"harness registry failed validation at {where}: {error.message}"


def load_harnesses_config(path: str | Path) -> dict[str, HarnessConfig]:
    """Parse and schema-validate the harness registry.

    Returns a mapping of harness name -> :class:`HarnessConfig`. Raises
    :class:`HarnessError` with an actionable message when the file is malformed,
    fails schema validation, or names a ``default`` that is not defined.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise HarnessError(
            f"harness registry must be a mapping, got {type(raw).__name__}"
        )

    validator = Draft202012Validator(HARNESS_REGISTRY_SCHEMA)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        primary = best_match(errors) or errors[0]
        raise HarnessError(_format_error(primary))

    registry: dict[str, HarnessConfig] = {}
    for name, settings in raw["harnesses"].items():
        registry[str(name)] = HarnessConfig(
            name=str(name),
            command=str(settings["command"]),
            parser=str(settings["parser"]),
            flags=list(settings.get("flags", [])),
            capabilities=dict(settings.get("capabilities", {})),
            enabled=bool(settings.get("enabled", True)),
            source="registry",
            probe=(str(settings["probe"]) if settings.get("probe") else None),
        )

    default = raw.get("default")
    if default is not None and default not in registry:
        raise HarnessError(
            f"default harness {default!r} is not defined in 'harnesses'"
        )
    return registry


def _builtin_harness() -> HarnessConfig:
    """The default Claude harness — argv resolves through the dispatch seam."""
    return HarnessConfig(
        name=DEFAULT_HARNESS,
        command=" ".join(DEFAULT_AGENT_CMD),
        parser=_CLAUDE_PARSER,
        capabilities=dict(_BUILTIN_CAPABILITIES),
        source="builtin",
    )


def _adhoc_env_harness(command: str) -> HarnessConfig:
    """Re-express an ``SDLC_AGENT_CMD`` override as an ad-hoc registry entry (AC3)."""
    return HarnessConfig(
        name=DEFAULT_HARNESS,
        command=command,
        parser=_CLAUDE_PARSER,
        capabilities=dict(_BUILTIN_CAPABILITIES),
        source="env",
    )


def resolve_harness(
    name: str | None = None,
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> HarnessConfig:
    """Resolve a harness to dispatch to.

    Precedence for the **default slot** (``name`` is ``None`` or ``"claude"``):
    ``SDLC_AGENT_CMD`` override (re-expressed as an ad-hoc ``env`` entry, AC3) →
    the built-in Claude default (AC2). The default slot deliberately does **not**
    consult ``harnesses.yaml`` for its argv, so behaviour with no registry wired
    is byte-identical to today's dispatch.

    A **named non-default** harness is resolved from the registry at
    ``config_path``; an absent ``config_path`` or an unknown name is a
    :class:`HarnessError` (fail fast, no half-run).
    """
    env_map = os.environ if env is None else env

    if name is not None and name != DEFAULT_HARNESS:
        if config_path is None:
            raise HarnessError(
                f"harness {name!r} requires a registry config, but none was supplied"
            )
        registry = load_harnesses_config(config_path)
        if name not in registry:
            known = ", ".join(sorted(registry)) or "(none)"
            raise HarnessError(
                f"unknown harness {name!r}; known harnesses: {known}"
            )
        return registry[name]

    override = env_map.get("SDLC_AGENT_CMD")
    if override:
        return _adhoc_env_harness(override)
    return _builtin_harness()


def resolve_agent_argv(
    name: str | None = None,
    *,
    config_path: str | Path | None = None,
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """The argv to launch an agent on the resolved harness (convenience wrapper)."""
    harness = resolve_harness(name, config_path=config_path, env=env)
    return harness.to_argv(model=model)
