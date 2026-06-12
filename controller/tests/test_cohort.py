# ABOUTME: Tests for deterministic dependency-cohort computation (Story 7.3-001).
# ABOUTME: Cohorts group stories whose dependencies are all in earlier cohorts.

from __future__ import annotations

import pytest

from sdlc.cohort import Story, compute_cohorts, truncate_queue


def _story(story_id: str, deps: list[str] | None = None, **kw) -> Story:
    return Story(
        id=story_id,
        title=kw.get("title", f"Story {story_id}"),
        epic_id=kw.get("epic_id", "07"),
        epic_name=kw.get("epic_name", "external-controller"),
        epic_file=kw.get("epic_file", "docs/stories/epic-07.md"),
        priority=kw.get("priority", "P2"),
        points=kw.get("points", 3),
        agent_type=kw.get("agent_type", "python-backend-engineer"),
        dependencies=list(deps or []),
    )


# ---------------------------------------------------------------------------
# compute_cohorts: dependency-respecting topological grouping
# ---------------------------------------------------------------------------

def test_no_dependencies_single_cohort() -> None:
    """Stories with no dependencies all land in cohort 1."""
    queue = [_story("7.1-001"), _story("7.2-001"), _story("7.3-001")]
    cohorts = compute_cohorts(queue)
    assert len(cohorts) == 1
    assert {s.id for s in cohorts[0]} == {"7.1-001", "7.2-001", "7.3-001"}


def test_linear_chain_produces_one_cohort_per_story() -> None:
    """A → B → C yields three sequential cohorts."""
    queue = [
        _story("A"),
        _story("B", deps=["A"]),
        _story("C", deps=["B"]),
    ]
    cohorts = compute_cohorts(queue)
    assert [[s.id for s in c] for c in cohorts] == [["A"], ["B"], ["C"]]


def test_diamond_dependency_groups_correctly() -> None:
    """A; B,C depend on A; D depends on B,C → 3 cohorts."""
    queue = [
        _story("A"),
        _story("B", deps=["A"]),
        _story("C", deps=["A"]),
        _story("D", deps=["B", "C"]),
    ]
    cohorts = compute_cohorts(queue)
    assert [s.id for s in cohorts[0]] == ["A"]
    assert {s.id for s in cohorts[1]} == {"B", "C"}
    assert [s.id for s in cohorts[2]] == ["D"]


def test_cohort_order_within_group_is_deterministic() -> None:
    """Stories within a cohort are sorted by id for reproducibility."""
    queue = [_story("7.3-002"), _story("7.1-001"), _story("7.2-001")]
    cohorts = compute_cohorts(queue)
    assert [s.id for s in cohorts[0]] == ["7.1-001", "7.2-001", "7.3-002"]


def test_dependency_outside_queue_is_ignored() -> None:
    """A dependency on a story not in the queue (already merged) is satisfied."""
    queue = [_story("B", deps=["A-already-merged"])]
    cohorts = compute_cohorts(queue)
    assert [s.id for s in cohorts[0]] == ["B"]


def test_cycle_raises_value_error() -> None:
    """A dependency cycle is a hard error, not an infinite loop."""
    queue = [_story("A", deps=["B"]), _story("B", deps=["A"])]
    with pytest.raises(ValueError, match="cycle"):
        compute_cohorts(queue)


def test_empty_queue_returns_empty_cohorts() -> None:
    """An empty queue produces no cohorts."""
    assert compute_cohorts([]) == []


# ---------------------------------------------------------------------------
# truncate_queue: --limit=N with dependency-integrity preservation
# ---------------------------------------------------------------------------

def test_truncate_keeps_first_n() -> None:
    """--limit=2 keeps the first two independent stories."""
    queue = [_story("A"), _story("B"), _story("C")]
    truncated = truncate_queue(queue, 2)
    assert {s.id for s in truncated} == {"A", "B"}


def test_truncate_pulls_in_missing_dependency() -> None:
    """If the cut would split a dependency pair, the dependency is included."""
    # B depends on C; limit=1 would keep only B, but C must come too.
    queue = [_story("B", deps=["C"]), _story("C")]
    truncated = truncate_queue(queue, 1)
    assert {s.id for s in truncated} == {"B", "C"}


def test_truncate_limit_zero_or_negative_returns_all() -> None:
    """A non-positive limit is a no-op (build everything)."""
    queue = [_story("A"), _story("B")]
    assert len(truncate_queue(queue, 0)) == 2
    assert len(truncate_queue(queue, -1)) == 2


def test_truncate_limit_larger_than_queue_returns_all() -> None:
    """A limit beyond the queue size returns the whole queue."""
    queue = [_story("A"), _story("B")]
    assert len(truncate_queue(queue, 99)) == 2
