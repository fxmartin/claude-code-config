# ABOUTME: Skill generator/transpiler — emit Claude and Codex SKILL.md files from one neutral source (Story 20.4-002).
# ABOUTME: One authored neutral source generates both harness files in lock-step, killing the hand-maintained mirror drift.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sdlc.skill_format import (
    NeutralSkill,
    SkillFormatError,
    parse_neutral_skill,
    render_body,
)

# Where each harness expects a skill to live, relative to its repo root. The
# Claude plugin and the Codex mirror happen to share the same sub-path, but they
# live in different repos (Codex under the `nix-install` parent), so the bases
# are passed in separately rather than derived from one another.
CLAUDE_SKILLS_SUBPATH = "plugins/autonomous-sdlc/skills"
CODEX_SKILLS_SUBPATH = "plugins/autonomous-sdlc/skills"


class SkillGeneratorError(SkillFormatError):
    """A neutral source could not be turned into a harness skill file."""


@dataclass(frozen=True)
class GeneratedSkill:
    """Where a neutral source's generated files landed.

    ``claude_path``/``codex_path`` are ``None`` when the skill does not target
    that harness (``harnesses`` in the source omits it).
    """

    name: str
    claude_path: Path | None
    codex_path: Path | None


def _title(name: str) -> str:
    """Render a kebab-case skill name as a Title-Case heading (``coverage`` →
    ``Coverage``; ``check-releases`` → ``Check Releases``)."""
    return " ".join(word.capitalize() for word in name.split("-"))


def claude_frontmatter(skill: NeutralSkill) -> dict[str, object]:
    """Map neutral metadata onto Claude's ``SKILL.md`` frontmatter keys.

    Only meaningful, non-default keys are emitted so the output stays terse and
    matches the hand-written Claude skill convention (``allowed-tools`` is a
    comma-joined string, not a YAML list).
    """
    meta = skill.metadata
    fm: dict[str, object] = {"name": meta.name, "description": meta.description}
    if meta.user_invocable is not True:
        fm["user-invocable"] = meta.user_invocable
    if meta.model_invocation == "disabled":
        fm["disable-model-invocation"] = True
    if meta.argument_hint is not None:
        fm["argument-hint"] = meta.argument_hint
    if meta.allowed_tools:
        fm["allowed-tools"] = ", ".join(meta.allowed_tools)
    return fm


def codex_frontmatter(skill: NeutralSkill) -> dict[str, object]:
    """Map neutral metadata onto the Codex ``.codex-plugin`` skill frontmatter.

    Codex carries the terse label under ``metadata.short-description`` rather
    than a top-level key, matching the mirror's manifest schema.
    """
    meta = skill.metadata
    fm: dict[str, object] = {"name": meta.name, "description": meta.description}
    if meta.short_description is not None:
        fm["metadata"] = {"short-description": meta.short_description}
    return fm


def _dump_frontmatter(data: dict[str, object]) -> str:
    """Serialise a frontmatter mapping to a ``---`` delimited block."""
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{body}\n---"


def _codex_invocation_lines(skill: NeutralSkill) -> list[str]:
    """The ``Use <skill> …`` invocation forms for the Codex skill body.

    Falls back to a bare ``Use <name>`` when the source declares no examples so
    every generated Codex skill always documents at least one invocation.
    """
    examples = skill.metadata.invocation_examples or (skill.metadata.name,)
    return [f"- `Use {example}`" for example in examples]


def generate_claude_skill(skill: NeutralSkill) -> str:
    """Emit the full Claude ``SKILL.md`` text for *skill*.

    The body is rendered for the ``claude`` target, so the Claude-only
    constructs (``$ARGUMENTS``, ``${CLAUDE_SKILL_DIR}``, the `` !`…` ``
    preprocessor) are restored exactly as in the hand-written original.
    """
    frontmatter = _dump_frontmatter(claude_frontmatter(skill))
    body = render_body(skill, "claude")
    return f"{frontmatter}\n\n{body}"


def generate_codex_skill(skill: NeutralSkill) -> str:
    """Emit the full Codex ``SKILL.md`` text for *skill*.

    Carries the Codex manifest-schema frontmatter, a ``# Title`` heading, the
    ``Use <skill> …`` invocation block, and the workflow body rendered for the
    ``codex`` target (Claude-only constructs dropped — Codex receives arguments
    via its ``Use <skill> …`` invocation).
    """
    frontmatter = _dump_frontmatter(codex_frontmatter(skill))
    title = _title(skill.metadata.name)
    invocation = "\n".join(_codex_invocation_lines(skill))
    body = render_body(skill, "codex").strip("\n")
    sections = [
        frontmatter,
        f"# {title}",
        f"This is a Codex-native port of the Claude Code "
        f"`{skill.metadata.name}` workflow.",
        "## Invocation",
        invocation,
        "Treat the user arguments as input to the workflow below.",
        body,
    ]
    return "\n\n".join(sections) + "\n"


def write_skill_files(
    skill: NeutralSkill, claude_base: Path, codex_base: Path
) -> GeneratedSkill:
    """Generate and write a skill's files under each targeted harness base.

    Files land at ``<base>/<name>/SKILL.md``. A harness the source does not
    target (per its ``harnesses`` list) is skipped and its path is ``None``.
    """
    name = skill.metadata.name
    claude_path: Path | None = None
    codex_path: Path | None = None

    if "claude" in skill.metadata.harnesses:
        target = Path(claude_base) / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generate_claude_skill(skill), encoding="utf-8")
        claude_path = target

    if "codex" in skill.metadata.harnesses:
        target = Path(codex_base) / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generate_codex_skill(skill), encoding="utf-8")
        codex_path = target

    return GeneratedSkill(name=name, claude_path=claude_path, codex_path=codex_path)


def load_neutral_skills(neutral_dir: Path) -> list[NeutralSkill]:
    """Parse every ``*.skill.md`` neutral source in *neutral_dir*, sorted by name.

    Raises :class:`SkillGeneratorError` (with the offending file named) if any
    source fails to parse, so a malformed authored skill fails loudly instead of
    silently dropping out of the generated set.
    """
    neutral_dir = Path(neutral_dir)
    if not neutral_dir.is_dir():
        raise FileNotFoundError(f"neutral skill directory not found: {neutral_dir}")

    skills: list[NeutralSkill] = []
    for path in sorted(neutral_dir.glob("*.skill.md")):
        try:
            skills.append(parse_neutral_skill(path.read_text(encoding="utf-8")))
        except SkillFormatError as exc:
            raise SkillGeneratorError(f"{path.name}: {exc}") from exc
    return skills


def generate_all(
    neutral_dir: Path, claude_base: Path, codex_base: Path
) -> list[GeneratedSkill]:
    """Generate every neutral source under *neutral_dir* into both harness bases."""
    return [
        write_skill_files(skill, claude_base, codex_base)
        for skill in load_neutral_skills(neutral_dir)
    ]
