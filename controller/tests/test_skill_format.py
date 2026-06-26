# ABOUTME: Tests for the harness-neutral skill definition format (Story 20.4-001).
# ABOUTME: Schema validity, parse/serialize round-trip, placeholders/harness blocks, and the 7 shared skills.

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sdlc.skill_format import (
    KNOWN_HARNESSES,
    KNOWN_PLACEHOLDERS,
    SKILL_METADATA_SCHEMA,
    HarnessBlock,
    NeutralSkill,
    SkillBodyError,
    SkillMetadata,
    SkillMetadataError,
    dump_neutral_skill,
    from_legacy_body,
    harness_blocks,
    load_skill_schema,
    parse_neutral_skill,
    placeholders,
    render_body,
    validate_metadata,
)
from sdlc.sync import SHARED_SKILLS

# The shared-skills source-of-truth tree lives at the repo root, two levels up
# from this test file (tests/ -> controller/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED_DIR = _REPO_ROOT / "shared-skills"
_NEUTRAL_DIR = _SHARED_DIR / "neutral"


def _valid_meta() -> dict:
    return {"name": "demo", "description": "Use when demoing the neutral format."}


# ---------------------------------------------------------------------------
# Schema is a structurally valid draft 2020-12 contract
# ---------------------------------------------------------------------------


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(load_skill_schema())


def test_schema_declares_draft_2020_12() -> None:
    assert SKILL_METADATA_SCHEMA["$schema"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )


def test_load_skill_schema_is_cached() -> None:
    assert load_skill_schema() is load_skill_schema()


# ---------------------------------------------------------------------------
# Metadata validation
# ---------------------------------------------------------------------------


def test_minimal_metadata_validates() -> None:
    validate_metadata(_valid_meta())  # must not raise


def test_full_metadata_validates() -> None:
    validate_metadata(
        {
            "name": "coverage",
            "description": "Assess and improve test coverage.",
            "short_description": "Improve coverage",
            "argument_hint": "[scope]",
            "allowed_tools": ["Bash", "Read"],
            "model_invocation": "disabled",
            "user_invocable": False,
            "invocation_examples": ["coverage focus on lib/"],
            "harnesses": ["claude", "codex"],
        }
    )


def test_missing_required_name_fails_with_field() -> None:
    meta = _valid_meta()
    del meta["name"]
    with pytest.raises(SkillMetadataError) as exc:
        validate_metadata(meta)
    assert "name" in str(exc.value)


def test_unknown_field_is_rejected() -> None:
    meta = _valid_meta()
    meta["disable_model_invocation"] = True  # Claude spelling, not the neutral key
    with pytest.raises(SkillMetadataError):
        validate_metadata(meta)


def test_non_kebab_name_is_rejected() -> None:
    meta = _valid_meta()
    meta["name"] = "Not_Kebab"
    with pytest.raises(SkillMetadataError):
        validate_metadata(meta)


def test_unknown_harness_is_rejected() -> None:
    meta = _valid_meta()
    meta["harnesses"] = ["claude", "opencode"]
    with pytest.raises(SkillMetadataError):
        validate_metadata(meta)


def test_bad_model_invocation_enum_is_rejected() -> None:
    meta = _valid_meta()
    meta["model_invocation"] = "sometimes"
    with pytest.raises(SkillMetadataError):
        validate_metadata(meta)


def test_non_mapping_frontmatter_is_rejected() -> None:
    with pytest.raises(SkillMetadataError):
        validate_metadata(["not", "a", "mapping"])


# ---------------------------------------------------------------------------
# Parse / serialize round-trip
# ---------------------------------------------------------------------------


def test_parse_extracts_metadata_and_body() -> None:
    text = "---\nname: demo\ndescription: A demo.\n---\nBody line one.\n"
    skill = parse_neutral_skill(text)
    assert skill.metadata.name == "demo"
    assert skill.metadata.description == "A demo."
    assert skill.body == "Body line one.\n"


def test_parse_applies_metadata_defaults() -> None:
    skill = parse_neutral_skill("---\nname: demo\ndescription: d\n---\nbody")
    assert skill.metadata.model_invocation == "auto"
    assert skill.metadata.user_invocable is True
    assert skill.metadata.harnesses == ("claude", "codex")
    assert skill.metadata.allowed_tools == ()


def test_dump_then_parse_is_identity() -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(
            name="demo",
            description="A demo skill.",
            short_description="demo",
            argument_hint="[scope]",
            allowed_tools=("Bash",),
            model_invocation="disabled",
            user_invocable=False,
            invocation_examples=["demo now"],
            harnesses=["claude"],
        ),
        body="Some body with {{ARGUMENTS}}.\n",
    )
    assert parse_neutral_skill(dump_neutral_skill(skill)) == skill


def test_dump_omits_default_fields() -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(name="demo", description="d"), body="b"
    )
    text = dump_neutral_skill(skill)
    assert "model_invocation" not in text
    assert "user_invocable" not in text
    assert "harnesses" not in text
    assert "allowed_tools" not in text


def test_parse_requires_frontmatter() -> None:
    with pytest.raises(SkillMetadataError):
        parse_neutral_skill("No frontmatter here, just a body.")


def test_metadata_constructor_coerces_lists_to_tuples() -> None:
    meta = SkillMetadata(name="d", description="d", allowed_tools=["Bash", "Read"])
    assert meta.allowed_tools == ("Bash", "Read")


# ---------------------------------------------------------------------------
# Body: placeholders and harness-tagged blocks
# ---------------------------------------------------------------------------


def test_placeholders_listed_in_order() -> None:
    body = "Run {{SHELL:date}} in {{SKILL_DIR}} then {{ARGUMENTS}}."
    assert placeholders(body) == ["SHELL", "SKILL_DIR", "ARGUMENTS"]


def test_known_placeholders_are_the_three_constructs() -> None:
    assert set(KNOWN_PLACEHOLDERS) == {"ARGUMENTS", "SKILL_DIR", "SHELL"}


def test_unknown_placeholder_is_rejected_on_parse() -> None:
    text = "---\nname: d\ndescription: d\n---\nUse {{BOGUS}} here."
    with pytest.raises(SkillBodyError) as exc:
        parse_neutral_skill(text)
    assert "BOGUS" in str(exc.value)


def test_render_claude_substitutes_constructs() -> None:
    skill = parse_neutral_skill(
        "---\nname: d\ndescription: d\n---\n"
        "{{ARGUMENTS}} {{SKILL_DIR}} {{SHELL:ls -la}}"
    )
    rendered = render_body(skill, "claude")
    assert rendered == "$ARGUMENTS ${CLAUDE_SKILL_DIR} !`ls -la`"


def test_render_codex_drops_claude_only_constructs() -> None:
    skill = parse_neutral_skill(
        "---\nname: d\ndescription: d\n---\nargs:{{ARGUMENTS}}|dir:{{SKILL_DIR}}"
    )
    assert render_body(skill, "codex") == "args:|dir:"


def test_render_unknown_harness_raises() -> None:
    skill = parse_neutral_skill("---\nname: d\ndescription: d\n---\nbody")
    with pytest.raises(Exception):
        render_body(skill, "opencode")


def test_harness_block_kept_for_target_dropped_otherwise() -> None:
    body = (
        "before\n"
        "<!-- harness:claude -->\nclaude-only\n<!-- /harness -->\n"
        "<!-- harness:codex -->\ncodex-only\n<!-- /harness -->\n"
        "after"
    )
    skill = NeutralSkill(metadata=SkillMetadata(name="d", description="d"), body=body)
    claude = render_body(skill, "claude")
    codex = render_body(skill, "codex")
    assert "claude-only" in claude and "codex-only" not in claude
    assert "codex-only" in codex and "claude-only" not in codex


def test_harness_blocks_introspection() -> None:
    body = "<!-- harness:claude -->\nx\n<!-- /harness -->"
    blocks = harness_blocks(body)
    assert blocks == [HarnessBlock(harness="claude", content="x")]


def test_unknown_harness_block_is_rejected_on_parse() -> None:
    text = (
        "---\nname: d\ndescription: d\n---\n"
        "<!-- harness:opencode -->\nx\n<!-- /harness -->"
    )
    with pytest.raises(SkillBodyError) as exc:
        parse_neutral_skill(text)
    assert "opencode" in str(exc.value)


# ---------------------------------------------------------------------------
# AC: the 7 shared skills are expressible in the format without loss
# ---------------------------------------------------------------------------


def test_seven_neutral_sources_present() -> None:
    """Exactly the seven shared skills have a committed neutral source."""
    present = {p.name for p in _NEUTRAL_DIR.glob("*.skill.md")}
    expected = {f"{name}.skill.md" for name in SHARED_SKILLS}
    assert present == expected


@pytest.mark.parametrize("name", SHARED_SKILLS)
def test_shared_skill_neutral_source_round_trips(name: str) -> None:
    """Each neutral source parses, validates, and serialises back identically."""
    text = (_NEUTRAL_DIR / f"{name}.skill.md").read_text(encoding="utf-8")
    skill = parse_neutral_skill(text)
    assert skill.metadata.name == name
    # Round-trip through the schema and serializer.
    assert parse_neutral_skill(dump_neutral_skill(skill)) == skill


@pytest.mark.parametrize("name", SHARED_SKILLS)
def test_shared_skill_renders_back_to_live_body(name: str) -> None:
    """The neutral source reproduces the live shared-skills body without loss.

    This is the expressibility proof (AC #2) and a built-in parity guard: edit
    a shared skill without regenerating its neutral source and this fails.
    """
    live_body = (_SHARED_DIR / f"{name}.md").read_text(encoding="utf-8")
    skill = parse_neutral_skill((_NEUTRAL_DIR / f"{name}.skill.md").read_text("utf-8"))
    assert render_body(skill, "claude") == live_body


@pytest.mark.parametrize("name", SHARED_SKILLS)
def test_from_legacy_body_is_lossless_for_shared_skills(name: str) -> None:
    """Wrapping a live body via from_legacy_body renders back to the original."""
    live_body = (_SHARED_DIR / f"{name}.md").read_text(encoding="utf-8")
    meta = SkillMetadata(name=name, description=f"The {name} skill.")
    skill = from_legacy_body(meta, live_body)
    assert render_body(skill, "claude") == live_body


def test_neutral_sources_only_use_known_harnesses() -> None:
    for name in SHARED_SKILLS:
        skill = parse_neutral_skill(
            (_NEUTRAL_DIR / f"{name}.skill.md").read_text("utf-8")
        )
        for h in skill.metadata.harnesses:
            assert h in KNOWN_HARNESSES
