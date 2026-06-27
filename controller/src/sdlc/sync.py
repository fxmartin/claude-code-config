# ABOUTME: Codex mirror sync parity logic for the shared skill set (Story 7.4-001).
# ABOUTME: Pure filesystem comparison — the hermetic core behind `git submodule update --remote`.

from __future__ import annotations

import difflib
import enum
from dataclasses import dataclass
from pathlib import Path

from sdlc.skill_format import parse_neutral_skill, render_body
from sdlc.skill_generator import (
    PIPELINE_SKILLS,
    generate_claude_skill,
    generate_codex_skill,
)

# Neutral skill sources are authored as `<name>.skill.md` (Story 20.4-001); the
# generated Claude/Codex bodies they produce are the plain `<name>.md` skills.
_NEUTRAL_SUFFIX = ".skill.md"

# Full-SKILL.md generators keyed by harness, for the pipeline-skill parity gate
# (Story 20.7-002). Pipeline skills (e.g. build-stories) ship a real SKILL.md to
# each harness plugin tree rather than the body-only shared-skills mirror.
_FULL_SKILL_GENERATORS = {
    "claude": generate_claude_skill,
    "codex": generate_codex_skill,
}

# The seven Codex extras that became the single source-of-truth shared skill
# set (Story 7.4-001 AC #3). `claude-code-config` hosts them under
# `shared-skills/`; the `nix-install` Codex mirror consumes the same directory
# as a git submodule, so there is exactly one copy.
SHARED_SKILLS: tuple[str, ...] = (
    "check-releases",
    "coverage",
    "create-issue",
    "create-project-summary-stats",
    "plan-release-update",
    "project-review",
    "roast",
)

# Files in the shared-skills tree that are scaffolding, not skills.
_NON_SKILL_FILES = frozenset({"README.md"})


class SkillState(enum.Enum):
    """Parity verdict for a single shared skill across source and consumer."""

    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    MISSING_IN_CONSUMER = "missing_in_consumer"
    EXTRA_IN_CONSUMER = "extra_in_consumer"


@dataclass(frozen=True)
class SkillParity:
    """One skill's parity verdict between source and consumer."""

    name: str
    state: SkillState


@dataclass(frozen=True)
class SyncReport:
    """The full parity verdict for a source/consumer pair.

    ``in_sync`` is true only when every skill is :data:`SkillState.IN_SYNC`,
    i.e. the consumer submodule is an exact byte-for-byte mirror of the source.
    """

    skills: tuple[SkillParity, ...]

    @property
    def in_sync(self) -> bool:
        return all(s.state is SkillState.IN_SYNC for s in self.skills)


def discover_shared_skills(skills_dir: Path) -> dict[str, str]:
    """Map skill name → file contents for every ``*.md`` skill in *skills_dir*.

    ``README.md`` (the index) is excluded; only skill definitions are returned.
    Raises ``FileNotFoundError`` if *skills_dir* does not exist so a misconfigured
    submodule path fails loudly instead of reporting a phantom empty set.
    """
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"shared-skills directory not found: {skills_dir}")

    skills: dict[str, str] = {}
    for path in sorted(skills_dir.glob("*.md")):
        if path.name in _NON_SKILL_FILES:
            continue
        skills[path.stem] = path.read_text(encoding="utf-8")
    return skills


def parity_report(source_dir: Path, consumer_dir: Path) -> SyncReport:
    """Compare the source-of-truth skills against a consumer submodule checkout.

    The union of skill names from both sides is reported, sorted by name, so a
    drift, a skill the consumer never pulled, and a stale leftover in the
    consumer all surface in one pass.
    """
    source = discover_shared_skills(source_dir)
    consumer = discover_shared_skills(consumer_dir)

    verdicts: list[SkillParity] = []
    for name in sorted(set(source) | set(consumer)):
        in_source = name in source
        in_consumer = name in consumer
        if in_source and not in_consumer:
            state = SkillState.MISSING_IN_CONSUMER
        elif in_consumer and not in_source:
            state = SkillState.EXTRA_IN_CONSUMER
        elif source[name] == consumer[name]:
            state = SkillState.IN_SYNC
        else:
            state = SkillState.DRIFTED
        verdicts.append(SkillParity(name=name, state=state))

    return SyncReport(skills=tuple(verdicts))


# --- generated-skill parity gate (Story 20.4-003) ---------------------------
#
# The shared-skills byte-mirror check above guards that a *consumer* checkout
# matches the source tree. This second gate guards one level up: that each
# committed generated body (`shared-skills/<name>.md`) still matches what its
# harness-neutral source (`shared-skills/neutral/<name>.skill.md`) generates.
# It is what fails CI when a generated skill file drifts from its source, so the
# Claude and Codex harnesses can never silently diverge.


class GeneratedState(enum.Enum):
    """Parity verdict for one skill between its neutral source and committed body."""

    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    MISSING = "missing"  # neutral source exists, no committed generated body
    ORPHAN = "orphan"  # committed generated body with no neutral source


@dataclass(frozen=True)
class GeneratedParity:
    """One skill's generated-output parity verdict, with a diff when it drifted."""

    name: str
    state: GeneratedState
    diff: str = ""


@dataclass(frozen=True)
class GeneratedParityReport:
    """The full generated-output parity verdict for one harness.

    ``in_sync`` is true only when every skill is :data:`GeneratedState.IN_SYNC`,
    i.e. every committed body is exactly what its neutral source regenerates.
    """

    harness: str
    skills: tuple[GeneratedParity, ...]

    @property
    def in_sync(self) -> bool:
        return all(s.state is GeneratedState.IN_SYNC for s in self.skills)


def discover_neutral_skills(neutral_dir: Path) -> dict[str, str]:
    """Map skill name → neutral source text for every ``*.skill.md`` in *neutral_dir*.

    Raises ``FileNotFoundError`` if *neutral_dir* does not exist so a
    mis-pointed sources path fails loudly instead of reporting a phantom empty
    set. Plain ``*.md`` files (e.g. a README) are not neutral sources and are
    ignored — only the ``.skill.md`` suffix counts.

    :data:`~sdlc.skill_generator.PIPELINE_SKILLS` are excluded: they are full
    ``SKILL.md`` plugin skills, not body-only ``shared-skills/<name>.md``
    mirrors, so they are checked by :func:`pipeline_parity_report` instead — the
    body-mirror gate must not flag them as missing.
    """
    neutral_dir = Path(neutral_dir)
    if not neutral_dir.is_dir():
        raise FileNotFoundError(f"neutral skills directory not found: {neutral_dir}")

    sources: dict[str, str] = {}
    for path in sorted(neutral_dir.glob(f"*{_NEUTRAL_SUFFIX}")):
        name = path.name[: -len(_NEUTRAL_SUFFIX)]
        if name in PIPELINE_SKILLS:
            continue
        sources[name] = path.read_text(encoding="utf-8")
    return sources


def generated_parity_report(
    neutral_dir: Path, generated_dir: Path, *, harness: str = "claude"
) -> GeneratedParityReport:
    """Compare every committed generated body against its neutral source.

    For each skill, the body rendered from the neutral source for *harness* is
    compared against the committed ``<name>.md`` in *generated_dir*. The union
    of names from both sides is reported, sorted by name, so a drift, a source
    that was never generated, and a stale generated file all surface in one
    pass. Drifted skills carry a unified diff (committed → regenerated).

    Raises ``FileNotFoundError`` if either directory is missing and
    :class:`sdlc.skill_format.SkillFormatError` if a neutral source is malformed.
    """
    sources = discover_neutral_skills(neutral_dir)
    generated = discover_shared_skills(generated_dir)

    verdicts: list[GeneratedParity] = []
    for name in sorted(set(sources) | set(generated)):
        in_source = name in sources
        in_generated = name in generated
        if in_source and not in_generated:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.MISSING))
            continue
        if in_generated and not in_source:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.ORPHAN))
            continue

        expected = render_body(parse_neutral_skill(sources[name]), harness)
        actual = generated[name]
        if expected == actual:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.IN_SYNC))
        else:
            diff = "".join(
                difflib.unified_diff(
                    actual.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile=f"{name}.md (committed)",
                    tofile=f"{name}.md (regenerated)",
                )
            )
            verdicts.append(
                GeneratedParity(name=name, state=GeneratedState.DRIFTED, diff=diff)
            )

    return GeneratedParityReport(harness=harness, skills=tuple(verdicts))


def write_generated_skills(
    neutral_dir: Path, generated_dir: Path, *, harness: str = "claude"
) -> list[str]:
    """Render every neutral source into *generated_dir* as ``<name>.md``.

    This is the regenerate / ``--fix`` path the parity gate points at: it makes
    each committed body exactly what its neutral source produces, so a follow-up
    :func:`generated_parity_report` is in sync. Returns the skill names written,
    sorted. *generated_dir* is created if absent.
    """
    sources = discover_neutral_skills(neutral_dir)
    generated_dir = Path(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name, text in sources.items():
        body = render_body(parse_neutral_skill(text), harness)
        (generated_dir / f"{name}.md").write_text(body, encoding="utf-8")
        written.append(name)
    return sorted(written)


# --- pipeline-skill parity gate (Story 20.7-002) ----------------------------
#
# The body-mirror gate above guards the seven body-only utility skills. This
# third gate guards the *pipeline* skills (build-stories): full ``SKILL.md``
# files generated into each harness's plugin tree. A hand-edit to a committed
# pipeline SKILL.md drifts from its neutral source, so this is what fails CI and
# points the author back at the regenerate command.


def discover_pipeline_sources(neutral_dir: Path) -> dict[str, str]:
    """Map skill name → neutral source text for every pipeline skill in *neutral_dir*.

    The inverse of :func:`discover_neutral_skills`: it returns only the
    :data:`~sdlc.skill_generator.PIPELINE_SKILLS` (full ``SKILL.md`` skills),
    so the pipeline parity gate and the body-mirror gate partition the sources
    between them with no overlap.
    """
    neutral_dir = Path(neutral_dir)
    if not neutral_dir.is_dir():
        raise FileNotFoundError(f"neutral skills directory not found: {neutral_dir}")

    sources: dict[str, str] = {}
    for path in sorted(neutral_dir.glob(f"*{_NEUTRAL_SUFFIX}")):
        name = path.name[: -len(_NEUTRAL_SUFFIX)]
        if name in PIPELINE_SKILLS:
            sources[name] = path.read_text(encoding="utf-8")
    return sources


def _generate_full_skill(text: str, harness: str) -> str:
    """Render a neutral source to a full ``SKILL.md`` for *harness*."""
    try:
        generator = _FULL_SKILL_GENERATORS[harness]
    except KeyError as exc:
        valid = ", ".join(sorted(_FULL_SKILL_GENERATORS))
        raise ValueError(
            f"unknown harness {harness!r}; expected one of: {valid}"
        ) from exc
    return generator(parse_neutral_skill(text))


def pipeline_parity_report(
    neutral_dir: Path, skill_base: Path, *, harness: str = "claude"
) -> GeneratedParityReport:
    """Compare every committed pipeline ``SKILL.md`` against its neutral source.

    For each pipeline skill, the full ``SKILL.md`` rendered from the neutral
    source for *harness* is compared against the committed
    ``<skill_base>/<name>/SKILL.md``. The union of pipeline names from the
    sources and the skill base is reported, sorted by name, so drift, a source
    that was never generated, and a stale generated file all surface in one
    pass. Drifted skills carry a unified diff (committed → regenerated).

    Raises ``FileNotFoundError`` if *neutral_dir* is missing and
    :class:`sdlc.skill_format.SkillFormatError` if a neutral source is malformed.
    """
    sources = discover_pipeline_sources(neutral_dir)
    skill_base = Path(skill_base)

    committed: dict[str, str] = {}
    if skill_base.is_dir():
        for name in PIPELINE_SKILLS:
            skill_file = skill_base / name / "SKILL.md"
            if skill_file.is_file():
                committed[name] = skill_file.read_text(encoding="utf-8")

    verdicts: list[GeneratedParity] = []
    for name in sorted(set(sources) | set(committed)):
        in_source = name in sources
        in_committed = name in committed
        if in_source and not in_committed:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.MISSING))
            continue
        if in_committed and not in_source:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.ORPHAN))
            continue

        expected = _generate_full_skill(sources[name], harness)
        actual = committed[name]
        if expected == actual:
            verdicts.append(GeneratedParity(name=name, state=GeneratedState.IN_SYNC))
        else:
            diff = "".join(
                difflib.unified_diff(
                    actual.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile=f"{name}/SKILL.md (committed)",
                    tofile=f"{name}/SKILL.md (regenerated)",
                )
            )
            verdicts.append(
                GeneratedParity(name=name, state=GeneratedState.DRIFTED, diff=diff)
            )

    return GeneratedParityReport(harness=harness, skills=tuple(verdicts))


def write_pipeline_skills(
    neutral_dir: Path, skill_base: Path, *, harness: str = "claude"
) -> list[str]:
    """Render every pipeline neutral source into *skill_base* as ``<name>/SKILL.md``.

    The regenerate / ``--fix`` path the pipeline parity gate points at: it makes
    each committed pipeline ``SKILL.md`` exactly what its neutral source
    produces. Returns the skill names written, sorted. Per-skill directories are
    created as needed.
    """
    sources = discover_pipeline_sources(neutral_dir)
    skill_base = Path(skill_base)

    written: list[str] = []
    for name, text in sources.items():
        target = skill_base / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_generate_full_skill(text, harness), encoding="utf-8")
        written.append(name)
    return sorted(written)
