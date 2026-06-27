# ABOUTME: Tests for the in-process-agent boundary — cross-harness vs Claude-only skills (Story 20.6-002).
# ABOUTME: Covers classification, the fail-fast guard, the doc pointer, and the support matrix.

from __future__ import annotations

import re

import pytest

from sdlc.harness import DEFAULT_HARNESS, HarnessError
from sdlc.portability import (
    BOUNDARY_DOC,
    CLAUDE_ONLY_SKILLS,
    CROSS_HARNESS_SKILLS,
    SupportRow,
    assert_harness_supports_skill,
    is_claude_only_skill,
    is_cross_harness_skill,
    support_matrix,
)


def test_skill_sets_are_disjoint():
    """A skill is on exactly one side of the boundary, never both."""
    assert CROSS_HARNESS_SKILLS & CLAUDE_ONLY_SKILLS == frozenset()


def test_known_claude_only_and_cross_harness_skills():
    """The boundary names the in-process skills explicitly, by design."""
    assert "fix-issue" in CLAUDE_ONLY_SKILLS
    assert "resume-build-agents" in CLAUDE_ONLY_SKILLS
    assert "build-stories" in CROSS_HARNESS_SKILLS


@pytest.mark.parametrize("skill", sorted(CLAUDE_ONLY_SKILLS))
def test_is_claude_only_skill_true(skill):
    assert is_claude_only_skill(skill) is True
    assert is_cross_harness_skill(skill) is False


@pytest.mark.parametrize("skill", sorted(CROSS_HARNESS_SKILLS))
def test_is_cross_harness_skill_true(skill):
    assert is_cross_harness_skill(skill) is True
    assert is_claude_only_skill(skill) is False


def test_unknown_skill_classified_neither_way():
    assert is_claude_only_skill("totally-unknown") is False
    assert is_cross_harness_skill("totally-unknown") is False


@pytest.mark.parametrize(
    "token",
    ["fix-issue", "/fix-issue", "  Fix-Issue  ", "RESUME-BUILD-AGENTS"],
)
def test_classification_normalizes_slash_case_and_whitespace(token):
    assert is_claude_only_skill(token) is True


def test_assert_guard_is_noop_on_default_claude_harness():
    # No raise — the default harness IS Claude, so the boundary is never crossed.
    assert assert_harness_supports_skill("fix-issue", DEFAULT_HARNESS) is None
    assert assert_harness_supports_skill("fix-issue", "/Claude") is None


def test_assert_guard_is_noop_for_cross_harness_skill():
    assert assert_harness_supports_skill("build-stories", "codex") is None


def test_assert_guard_is_noop_for_unknown_skill():
    # The boundary makes no claim about a skill it does not know.
    assert assert_harness_supports_skill("totally-unknown", "codex") is None


@pytest.mark.parametrize("skill", sorted(CLAUDE_ONLY_SKILLS))
def test_assert_guard_fails_fast_for_claude_only_skill_on_other_harness(skill):
    with pytest.raises(HarnessError) as excinfo:
        assert_harness_supports_skill(skill, "codex")
    message = str(excinfo.value)
    assert skill in message
    assert "codex" in message
    assert "Claude-only" in message
    # The refusal must point the reader at the boundary doc.
    assert BOUNDARY_DOC in message


def test_assert_guard_message_points_at_architecture_doc():
    with pytest.raises(HarnessError) as excinfo:
        assert_harness_supports_skill("fix-issue", "codex")
    assert "docs/controller-architecture.md" in str(excinfo.value)


def test_support_matrix_covers_every_known_skill_once():
    rows = support_matrix()
    skills = [row.skill for row in rows]
    assert sorted(skills) == sorted(CROSS_HARNESS_SKILLS | CLAUDE_ONLY_SKILLS)
    # Sorted by skill for stable rendering.
    assert skills == sorted(skills)


def test_support_matrix_rows_flag_the_boundary_correctly():
    by_skill = {row.skill: row for row in support_matrix()}
    cross = by_skill["build-stories"]
    assert cross.cross_harness is True
    assert cross.harness_support == "Any registry harness"
    assert "dispatch seam" in cross.mechanism

    claude_only = by_skill["fix-issue"]
    assert claude_only.cross_harness is False
    assert claude_only.harness_support == "Claude only"
    assert "in-process Agent tool" in claude_only.mechanism


def test_support_row_harness_support_property_both_branches():
    assert SupportRow("x", cross_harness=True, mechanism="m").harness_support == (
        "Any registry harness"
    )
    assert SupportRow("y", cross_harness=False, mechanism="m").harness_support == (
        "Claude only"
    )


def test_boundary_doc_is_an_anchor_into_the_architecture_doc():
    assert re.match(r"docs/controller-architecture\.md#", BOUNDARY_DOC)
