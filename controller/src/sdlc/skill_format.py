# ABOUTME: Harness-neutral skill definition format — parse, validate, serialize, render (Story 20.4-001).
# ABOUTME: One source (frontmatter metadata + body with neutral placeholders) generates Claude and Codex skills.

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

# The metadata schema ships inside the package (alongside the Epic-07 agent
# schemas) so it resolves under `uv tool install` where the source tree is gone.
_SCHEMA_FILE = "neutral-skill.schema.json"

# Harnesses the format knows how to target. New harnesses (opencode, pi, …) are
# added here; the format itself needs no other change to recognise them.
KNOWN_HARNESSES: tuple[str, ...] = ("claude", "codex")
_DEFAULT_HARNESSES: tuple[str, ...] = ("claude", "codex")

# Neutral placeholder tokens that stand in for the three Claude-only constructs
# named in the story: `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`, and the `` !`…` ``
# shell preprocessor. The generator translates (or omits) each per target so a
# single body works on any harness.
KNOWN_PLACEHOLDERS: tuple[str, ...] = ("ARGUMENTS", "SKILL_DIR", "SHELL")

# `{{NAME}}` or `{{NAME:argument}}` (the argument carries the shell command for
# `{{SHELL:…}}`). Names are upper-snake to stay clear of prose braces.
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)(?::([^}]*))?\}\}")

# A harness-tagged block emits its body for one target only:
#     <!-- harness:claude -->
#     …claude-only content…
#     <!-- /harness -->
_HARNESS_BLOCK_RE = re.compile(
    r"[ \t]*<!--\s*harness:([a-z0-9-]+)\s*-->[ \t]*\n"
    r"(.*?)"
    r"\n[ \t]*<!--\s*/harness\s*-->[ \t]*(?:\n|$)",
    re.DOTALL,
)

# Frontmatter: a leading `---` line, the YAML block, a closing `---` line, then
# the body verbatim. Non-greedy so the *first* closing delimiter wins (a body
# may itself contain `---` horizontal rules).
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class SkillFormatError(Exception):
    """Base error for the neutral skill format."""


class SkillMetadataError(SkillFormatError):
    """The frontmatter metadata was missing, malformed, or schema-invalid."""


class SkillBodyError(SkillFormatError):
    """The body used an unknown placeholder or a malformed harness block."""


@lru_cache(maxsize=1)
def load_skill_schema() -> dict[str, Any]:
    """Load and cache the neutral-skill metadata JSON schema."""
    resource = resources.files(__package__) / "schemas" / _SCHEMA_FILE
    return json.loads(resource.read_text(encoding="utf-8"))


# Exposed for callers/tests that want to introspect the published contract.
SKILL_METADATA_SCHEMA: dict[str, Any] = load_skill_schema()


@dataclass(frozen=True)
class HarnessBlock:
    """One harness-tagged block: content emitted only for ``harness``."""

    harness: str
    content: str


@dataclass(frozen=True)
class SkillMetadata:
    """Harness-agnostic metadata for a neutral skill source.

    Captures the union of what the Claude ``SKILL.md`` frontmatter
    (``allowed-tools``, ``argument-hint``, ``disable-model-invocation``,
    ``user-invocable``) and the Codex ``.codex-plugin`` manifest / ``Use
    <skill>`` form need, without privileging either harness's spelling.
    """

    name: str
    description: str
    short_description: str | None = None
    argument_hint: str | None = None
    allowed_tools: tuple[str, ...] = ()
    model_invocation: str = "auto"
    user_invocable: bool = True
    invocation_examples: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = _DEFAULT_HARNESSES

    def __post_init__(self) -> None:
        # Coerce sequence fields to tuples so a list-built instance compares
        # equal to a parsed one (frozen dataclass: bypass via object.__setattr__).
        for seq_field in ("allowed_tools", "invocation_examples", "harnesses"):
            object.__setattr__(self, seq_field, tuple(getattr(self, seq_field)))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a frontmatter mapping, emitting only non-default fields.

        Defaults are omitted so a round-trip is stable and the source stays
        terse; :meth:`from_dict` restores them.
        """
        data: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.short_description is not None:
            data["short_description"] = self.short_description
        if self.argument_hint is not None:
            data["argument_hint"] = self.argument_hint
        if self.allowed_tools:
            data["allowed_tools"] = list(self.allowed_tools)
        if self.model_invocation != "auto":
            data["model_invocation"] = self.model_invocation
        if self.user_invocable is not True:
            data["user_invocable"] = self.user_invocable
        if self.invocation_examples:
            data["invocation_examples"] = list(self.invocation_examples)
        if tuple(self.harnesses) != _DEFAULT_HARNESSES:
            data["harnesses"] = list(self.harnesses)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillMetadata":
        """Build from a validated frontmatter mapping, applying defaults."""
        return cls(
            name=data["name"],
            description=data["description"],
            short_description=data.get("short_description"),
            argument_hint=data.get("argument_hint"),
            allowed_tools=tuple(data.get("allowed_tools", ())),
            model_invocation=data.get("model_invocation", "auto"),
            user_invocable=data.get("user_invocable", True),
            invocation_examples=tuple(data.get("invocation_examples", ())),
            harnesses=tuple(data.get("harnesses", _DEFAULT_HARNESSES)),
        )


@dataclass(frozen=True)
class NeutralSkill:
    """A parsed neutral skill source: metadata plus the harness-neutral body."""

    metadata: SkillMetadata
    body: str


def validate_metadata(data: Any) -> None:
    """Validate a frontmatter mapping against the metadata schema.

    Raises :class:`SkillMetadataError` with an actionable message naming the
    offending field on any failure.
    """
    if not isinstance(data, dict):
        kind = type(data).__name__
        raise SkillMetadataError(f"skill frontmatter must be a mapping, got {kind}")

    error = best_match(Draft202012Validator(load_skill_schema()).iter_errors(data))
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise SkillMetadataError(
            f"skill metadata invalid at {location}: {error.message}"
        )


def parse_neutral_skill(text: str) -> NeutralSkill:
    """Parse a neutral skill source (``---`` frontmatter + body).

    The body is preserved verbatim. Raises :class:`SkillMetadataError` when the
    frontmatter is missing/invalid and :class:`SkillBodyError` when the body
    references an unknown placeholder or an unknown harness tag.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillMetadataError(
            "skill source must start with a '---' YAML frontmatter block"
        )

    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:  # pragma: no cover - exercised via test
        raise SkillMetadataError(f"frontmatter is not valid YAML: {exc}") from exc

    validate_metadata(raw)
    body = match.group(2)
    _check_body(body)
    return NeutralSkill(metadata=SkillMetadata.from_dict(raw), body=body)


def dump_neutral_skill(skill: NeutralSkill) -> str:
    """Serialise a :class:`NeutralSkill` back to its source text.

    ``parse_neutral_skill(dump_neutral_skill(skill)) == skill`` for any skill
    this module produced.
    """
    yaml_text = yaml.safe_dump(
        skill.metadata.to_dict(), sort_keys=False, allow_unicode=True
    ).rstrip("\n")
    return f"---\n{yaml_text}\n---\n{skill.body}"


def placeholders(body: str) -> list[str]:
    """Return the placeholder names used in *body*, in order of appearance."""
    return [m.group(1) for m in _PLACEHOLDER_RE.finditer(body)]


def harness_blocks(body: str) -> list[HarnessBlock]:
    """Return the harness-tagged blocks found in *body*, in order."""
    return [
        HarnessBlock(harness=m.group(1), content=m.group(2))
        for m in _HARNESS_BLOCK_RE.finditer(body)
    ]


def _check_body(body: str) -> None:
    """Validate placeholder names and harness tags up front, with clear errors."""
    for name in placeholders(body):
        if name not in KNOWN_PLACEHOLDERS:
            valid = ", ".join(KNOWN_PLACEHOLDERS)
            raise SkillBodyError(
                f"unknown placeholder {{{{{name}}}}}; expected one of: {valid}"
            )
    for block in harness_blocks(body):
        if block.harness not in KNOWN_HARNESSES:
            valid = ", ".join(KNOWN_HARNESSES)
            raise SkillBodyError(
                f"unknown harness tag 'harness:{block.harness}'; expected one of: {valid}"
            )


def _render_placeholder(harness: str, name: str, arg: str | None) -> str:
    """Map one neutral placeholder to its concrete text for *harness*."""
    if harness == "claude":
        if name == "ARGUMENTS":
            return "$ARGUMENTS"
        if name == "SKILL_DIR":
            return "${CLAUDE_SKILL_DIR}"
        if name == "SHELL":
            return f"!`{arg or ''}`"
    # Codex (and any harness without these constructs): the args arrive via the
    # `Use <skill> …` invocation, and there is no skill-dir / shell preprocessor,
    # so the placeholders are dropped rather than mistranslated.
    return ""


def render_body(skill: NeutralSkill, harness: str) -> str:
    """Render the neutral body to concrete text for *harness*.

    Resolves harness-tagged blocks (keep for the target, drop otherwise) and
    substitutes neutral placeholders. This is the body-level semantics of the
    format; emitting whole skill *files* (frontmatter/manifest) is the
    generator's job (Story 20.4-002).
    """
    if harness not in KNOWN_HARNESSES:
        valid = ", ".join(KNOWN_HARNESSES)
        raise SkillFormatError(f"unknown harness {harness!r}; expected one of: {valid}")

    def _resolve_block(match: re.Match[str]) -> str:
        return match.group(2) if match.group(1) == harness else ""

    rendered = _HARNESS_BLOCK_RE.sub(_resolve_block, skill.body)

    def _resolve_placeholder(match: re.Match[str]) -> str:
        return _render_placeholder(harness, match.group(1), match.group(2))

    return _PLACEHOLDER_RE.sub(_resolve_placeholder, rendered)


def from_legacy_body(metadata: SkillMetadata, body: str) -> NeutralSkill:
    """Build a neutral skill from an existing plain-Claude skill *body*.

    Rewrites the Claude-only ``$ARGUMENTS`` token to the neutral
    ``{{ARGUMENTS}}`` placeholder so the body round-trips back to the original
    via :func:`render_body` with ``harness="claude"``. Used to prove the
    existing ``shared-skills/`` bodies are expressible without loss.
    """
    neutral_body = body.replace("$ARGUMENTS", "{{ARGUMENTS}}")
    skill = NeutralSkill(metadata=metadata, body=neutral_body)
    _check_body(skill.body)
    return skill
