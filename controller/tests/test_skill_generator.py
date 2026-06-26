# ABOUTME: Tests for the skill generator/transpiler (Story 20.4-002).
# ABOUTME: Golden Claude + Codex output, body parity with the live skill, harness targeting, and the CLI verb.

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.skill_format import (
    NeutralSkill,
    SkillMetadata,
    parse_neutral_skill,
)
from sdlc.skill_generator import (
    GeneratedSkill,
    SkillGeneratorError,
    claude_frontmatter,
    codex_frontmatter,
    generate_all,
    generate_claude_skill,
    generate_codex_skill,
    load_neutral_skills,
    write_skill_files,
)

# tests/ -> controller/ -> repo root holds shared-skills/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEUTRAL_DIR = _REPO_ROOT / "shared-skills" / "neutral"
_SHARED_DIR = _REPO_ROOT / "shared-skills"
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

runner = CliRunner()


def _coverage_skill() -> NeutralSkill:
    return parse_neutral_skill(
        (_NEUTRAL_DIR / "coverage.skill.md").read_text(encoding="utf-8")
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (parsed frontmatter mapping, body) for a generated SKILL.md."""
    assert text.startswith("---\n")
    head, rest = text.split("\n---\n", 1)  # head == "---\n<frontmatter>"
    frontmatter = head[len("---\n") :]
    body = rest.lstrip("\n")  # drop the blank line between frontmatter and body
    return yaml.safe_load(frontmatter), body


# ---------------------------------------------------------------------------
# Golden output (AC #2 / #3, DoD: golden Claude + Codex for a representative skill)
# ---------------------------------------------------------------------------


def test_claude_golden_matches() -> None:
    expected = (_GOLDEN_DIR / "coverage.claude.SKILL.md").read_text(encoding="utf-8")
    assert generate_claude_skill(_coverage_skill()) == expected


def test_codex_golden_matches() -> None:
    expected = (_GOLDEN_DIR / "coverage.codex.SKILL.md").read_text(encoding="utf-8")
    assert generate_codex_skill(_coverage_skill()) == expected


def test_claude_body_is_identical_to_live_skill() -> None:
    """AC #2: the generated Claude body reproduces the hand-written skill body.

    The live ``shared-skills/coverage.md`` is the current hand-written body; the
    generated Claude ``SKILL.md`` must carry it verbatim (constructs restored).
    """
    live_body = (_SHARED_DIR / "coverage.md").read_text(encoding="utf-8")
    _, body = _split_frontmatter(generate_claude_skill(_coverage_skill()))
    assert body == live_body


# ---------------------------------------------------------------------------
# Claude frontmatter mapping
# ---------------------------------------------------------------------------


def test_claude_frontmatter_minimal_keys() -> None:
    fm = claude_frontmatter(_coverage_skill())
    assert fm == {
        "name": "coverage",
        "description": _coverage_skill().metadata.description,
    }


def test_claude_frontmatter_full_mapping() -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(
            name="demo",
            description="d",
            argument_hint="[scope]",
            allowed_tools=("Read", "Write", "Bash"),
            model_invocation="disabled",
            user_invocable=False,
        ),
        body="body {{ARGUMENTS}}",
    )
    fm = claude_frontmatter(skill)
    assert fm["user-invocable"] is False
    assert fm["disable-model-invocation"] is True
    assert fm["argument-hint"] == "[scope]"
    # allowed-tools is a comma-joined string, matching the Claude convention.
    assert fm["allowed-tools"] == "Read, Write, Bash"


def test_claude_skill_frontmatter_round_trips() -> None:
    fm, _ = _split_frontmatter(generate_claude_skill(_coverage_skill()))
    assert fm["name"] == "coverage"
    assert fm["description"]


# ---------------------------------------------------------------------------
# Codex frontmatter + body shape (AC #3)
# ---------------------------------------------------------------------------


def test_codex_frontmatter_carries_short_description_under_metadata() -> None:
    fm = codex_frontmatter(_coverage_skill())
    assert fm["name"] == "coverage"
    assert fm["metadata"] == {
        "short-description": "Improve test coverage pragmatically"
    }


def test_codex_frontmatter_omits_metadata_when_no_short_description() -> None:
    skill = NeutralSkill(metadata=SkillMetadata(name="demo", description="d"), body="b")
    assert "metadata" not in codex_frontmatter(skill)


def test_codex_skill_has_manifest_frontmatter_and_use_invocation() -> None:
    text = generate_codex_skill(_coverage_skill())
    fm, body = _split_frontmatter(text)
    assert fm["name"] == "coverage"
    assert fm["description"]
    assert fm["metadata"]["short-description"]
    # `Use <skill>` invocation forms are present.
    assert "- `Use coverage`" in body
    assert "- `Use coverage for bootstrap scripts`" in body
    assert "# Coverage" in body


def test_codex_drops_claude_only_constructs() -> None:
    text = generate_codex_skill(_coverage_skill())
    assert "$ARGUMENTS" not in text


def test_codex_invocation_falls_back_to_bare_use_name() -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(name="demo", description="d"), body="workflow"
    )
    assert "- `Use demo`" in generate_codex_skill(skill)


def test_codex_title_handles_multi_word_names() -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(name="check-releases", description="d"), body="x"
    )
    assert "# Check Releases" in generate_codex_skill(skill)


# ---------------------------------------------------------------------------
# Writing files + harness targeting
# ---------------------------------------------------------------------------


def test_write_skill_files_writes_both_harnesses(tmp_path: Path) -> None:
    claude_base = tmp_path / "claude"
    codex_base = tmp_path / "codex"
    result = write_skill_files(_coverage_skill(), claude_base, codex_base)
    assert isinstance(result, GeneratedSkill)
    assert result.claude_path == claude_base / "coverage" / "SKILL.md"
    assert result.codex_path == codex_base / "coverage" / "SKILL.md"
    assert result.claude_path.read_text(encoding="utf-8").startswith("---\n")
    assert result.codex_path.read_text(encoding="utf-8").startswith("---\n")


def test_write_skill_files_skips_untargeted_harness(tmp_path: Path) -> None:
    skill = NeutralSkill(
        metadata=SkillMetadata(
            name="claude-only", description="d", harnesses=("claude",)
        ),
        body="b",
    )
    result = write_skill_files(skill, tmp_path / "claude", tmp_path / "codex")
    assert result.claude_path is not None and result.claude_path.exists()
    assert result.codex_path is None
    assert not (tmp_path / "codex" / "claude-only").exists()


def test_write_skill_files_skips_untargeted_claude_harness(tmp_path: Path) -> None:
    """The mirror case: a codex-only source skips Claude entirely."""
    skill = NeutralSkill(
        metadata=SkillMetadata(
            name="codex-only", description="d", harnesses=("codex",)
        ),
        body="b",
    )
    result = write_skill_files(skill, tmp_path / "claude", tmp_path / "codex")
    assert result.codex_path is not None and result.codex_path.exists()
    assert result.claude_path is None
    assert not (tmp_path / "claude" / "codex-only").exists()


def test_generate_all_covers_every_neutral_source(tmp_path: Path) -> None:
    generated = generate_all(_NEUTRAL_DIR, tmp_path / "claude", tmp_path / "codex")
    names = {g.name for g in generated}
    expected = {p.stem.removesuffix(".skill") for p in _NEUTRAL_DIR.glob("*.skill.md")}
    assert names == expected
    for g in generated:
        assert g.claude_path is not None and g.claude_path.exists()
        assert g.codex_path is not None and g.codex_path.exists()


# ---------------------------------------------------------------------------
# Loading / error handling
# ---------------------------------------------------------------------------


def test_load_neutral_skills_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_neutral_skills(tmp_path / "nope")


def test_load_neutral_skills_bad_source_names_file(tmp_path: Path) -> None:
    (tmp_path / "broken.skill.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(SkillGeneratorError) as exc:
        load_neutral_skills(tmp_path)
    assert "broken.skill.md" in str(exc.value)


# ---------------------------------------------------------------------------
# CLI verb
# ---------------------------------------------------------------------------


def test_cli_generate_skills_writes_files(tmp_path: Path) -> None:
    claude_base = tmp_path / "claude"
    codex_base = tmp_path / "codex"
    result = runner.invoke(
        app,
        [
            "generate-skills",
            str(_NEUTRAL_DIR),
            "--claude-base",
            str(claude_base),
            "--codex-base",
            str(codex_base),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "generated" in result.output
    assert (claude_base / "coverage" / "SKILL.md").exists()
    assert (codex_base / "coverage" / "SKILL.md").exists()


def test_cli_generate_skills_missing_dir_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "generate-skills",
            str(tmp_path / "nope"),
            "--claude-base",
            str(tmp_path / "c"),
            "--codex-base",
            str(tmp_path / "x"),
        ],
    )
    assert result.exit_code == 2
    assert "error:" in result.output
