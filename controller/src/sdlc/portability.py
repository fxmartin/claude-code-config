# ABOUTME: The in-process-agent boundary — which skills are cross-harness vs Claude-only.
# ABOUTME: Story 20.6-002 — documents and enforces that fix-issue/resume-build-agents stay Claude-only.

from __future__ import annotations

from dataclasses import dataclass

from sdlc.harness import DEFAULT_HARNESS, HarnessError

# Where the *why* lives. Error messages point here so a reader who hits the
# boundary lands on the explanation (in-process Agent tool has no CLI equivalent)
# rather than just a bare refusal. Repo-root-relative so it resolves from a clone.
BOUNDARY_DOC = "docs/controller-architecture.md#the-in-process-agent-boundary-story-206-002"

# Skills that drive the controller's dispatch seam (sdlc/dispatch.py): they
# assemble a prompt and shell out, so a role can be routed to any registry
# harness (claude, codex, …). The build-stories orchestration is cross-harness.
CROSS_HARNESS_SKILLS: frozenset[str] = frozenset({"build-stories"})

# Skills that spawn Claude **in-process** via the Agent tool
# (`subagent_type`/`model`/`isolation="worktree"`). That tool is a Claude Code
# primitive with no CLI-harness equivalent, so these skills are Claude-only by
# design — there is nothing to port (the Epic-20 boundary).
CLAUDE_ONLY_SKILLS: frozenset[str] = frozenset({"fix-issue", "resume-build-agents"})

# Invariant: a skill is on exactly one side of the boundary, never both.
assert not (CROSS_HARNESS_SKILLS & CLAUDE_ONLY_SKILLS), (
    "a skill cannot be both cross-harness and Claude-only"
)


@dataclass(frozen=True)
class SupportRow:
    """One row of the harness-support matrix: a skill and how it dispatches."""

    skill: str
    cross_harness: bool
    mechanism: str

    @property
    def harness_support(self) -> str:
        """Human-readable harness support for this skill."""
        return "Any registry harness" if self.cross_harness else "Claude only"


def _normalize(name: str) -> str:
    """Lower-case and strip a skill/harness token, tolerating a leading slash."""
    return name.strip().lstrip("/").lower()


def is_claude_only_skill(skill: str) -> bool:
    """True when *skill* spawns in-process Claude agents and cannot be ported."""
    return _normalize(skill) in CLAUDE_ONLY_SKILLS


def is_cross_harness_skill(skill: str) -> bool:
    """True when *skill* dispatches through the harness seam (any harness)."""
    return _normalize(skill) in CROSS_HARNESS_SKILLS


def assert_harness_supports_skill(skill: str, harness: str) -> None:
    """Fail fast when a Claude-only skill is asked to run on a non-Claude harness.

    A no-op for the default Claude harness, for a cross-harness skill, or for an
    unknown skill (the boundary makes no claim about it). For a Claude-only skill
    on any other harness it raises :class:`HarnessError` pointing at
    :data:`BOUNDARY_DOC`, so the caller fails before doing half a run.
    """
    if _normalize(harness) == DEFAULT_HARNESS:
        return
    if is_claude_only_skill(skill):
        raise HarnessError(
            f"skill {_normalize(skill)!r} is Claude-only: it spawns in-process "
            f"Agent sub-agents (subagent_type/isolation) with no CLI-harness "
            f"equivalent, so it cannot run on harness {_normalize(harness)!r}. "
            f"See {BOUNDARY_DOC}."
        )


def support_matrix() -> tuple[SupportRow, ...]:
    """The harness-support matrix, sorted by skill, for rendering and tests."""
    rows = [
        SupportRow(
            skill,
            cross_harness=True,
            mechanism="controller dispatch seam (sdlc/dispatch.py)",
        )
        for skill in CROSS_HARNESS_SKILLS
    ] + [
        SupportRow(
            skill,
            cross_harness=False,
            mechanism="in-process Agent tool (subagent_type/isolation)",
        )
        for skill in CLAUDE_ONLY_SKILLS
    ]
    return tuple(sorted(rows, key=lambda row: row.skill))
