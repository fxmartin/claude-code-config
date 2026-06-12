# ABOUTME: Deterministic dependency-cohort computation for the build state machine.
# ABOUTME: Story 7.3-001 — groups stories whose dependencies are all already built.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Story:
    """One unit of work scheduled within a build run.

    Mirrors the ``QUEUE_JSON:`` records the discovery agent emits today, so the
    controller's queue parser stays compatible with the existing skill output.
    """

    id: str
    title: str
    epic_id: str
    epic_name: str
    epic_file: str
    priority: str
    points: int
    agent_type: str
    dependencies: list[str] = field(default_factory=list)


def compute_cohorts(queue: list[Story]) -> list[list[Story]]:
    """Group ``queue`` into dependency cohorts.

    A cohort contains every story whose dependencies are either absent from the
    queue (already merged in a prior run) or were placed in an earlier cohort.
    Stories within a cohort are sorted by ``id`` so the schedule is reproducible
    across invocations.

    Raises :class:`ValueError` when the dependency graph contains a cycle —
    that is a hard error, never an infinite loop.
    """
    if not queue:
        return []

    in_queue = {s.id for s in queue}
    # Only intra-queue edges matter; dependencies on already-merged stories are
    # considered satisfied from the outset.
    pending = {
        s.id: {dep for dep in s.dependencies if dep in in_queue} for s in queue
    }
    by_id = {s.id: s for s in queue}

    cohorts: list[list[Story]] = []
    satisfied: set[str] = set()

    while pending:
        ready = sorted(
            sid for sid, deps in pending.items() if deps <= satisfied
        )
        if not ready:
            remaining = ", ".join(sorted(pending))
            raise ValueError(
                f"dependency cycle (or unresolvable dependency) among: {remaining}"
            )
        cohorts.append([by_id[sid] for sid in ready])
        for sid in ready:
            del pending[sid]
            satisfied.add(sid)

    return cohorts


def truncate_queue(queue: list[Story], limit: int) -> list[Story]:
    """Return at most ``limit`` stories, preserving dependency integrity.

    A non-positive ``limit`` (or one at least as large as the queue) is a no-op.
    If the cut would orphan a dependency (story A kept but its in-queue
    dependency B dropped), B is pulled back in even when that exceeds ``limit``
    — the same rule the skill uses today.
    """
    if limit <= 0 or limit >= len(queue):
        return list(queue)

    by_id = {s.id: s for s in queue}
    kept_ids: set[str] = set()
    kept: list[Story] = []
    for story in queue[:limit]:
        kept.append(story)
        kept_ids.add(story.id)

    # Pull in any in-queue dependency of a kept story that was cut.
    frontier = list(kept)
    while frontier:
        story = frontier.pop()
        for dep in story.dependencies:
            if dep in by_id and dep not in kept_ids:
                dep_story = by_id[dep]
                kept.append(dep_story)
                kept_ids.add(dep)
                frontier.append(dep_story)

    # Preserve the original queue order for determinism.
    return [s for s in queue if s.id in kept_ids]
