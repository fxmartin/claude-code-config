# ABOUTME: Codex mirror sync parity logic for the shared skill set (Story 7.4-001).
# ABOUTME: Pure filesystem comparison — the hermetic core behind `git submodule update --remote`.

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

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
