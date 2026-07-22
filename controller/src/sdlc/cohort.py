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
    # Story 28.2-001: `points` is a **descriptive scope label** — a human-facing
    # size hint carried onto the story doc, the issue (`points:N`) and the
    # dashboard. It is not a predictor feature: the 2026-07-19 dataset showed
    # points do not predict cost on this factory's own work, and 172/193 builds
    # carried the same two values. Machine decisions read the features below (and,
    # from Story 28.2-002, the prediction derived from them) instead.
    points: int
    agent_type: str
    dependencies: list[str] = field(default_factory=list)
    # True when the epic marks the story shipped (Status: Done, or all DoD boxes
    # checked). The build skips these by default so an epic-scoped run never
    # re-opens merged work — see run_build's done-skip and `--rebuild`.
    done: bool = False
    # The story's own markdown section (header line through the line before the
    # next heading), captured at discovery-parse time so the build/coverage
    # prompts can embed the spec instead of sending the agent to re-read the
    # epic (Story 27.3-002). Empty for synthesized stories (fix-issue) — the
    # renderers then fall back to the read-it-yourself instruction.
    section: str = ""
    # Story 28.2-001: predictor features extracted at discovery time — the inputs
    # the cost/rework predictor (Story 28.2-002) keys on instead of `points`.
    # ``None`` means **unknown**, never zero: the epic did not state enough to
    # compute the feature, so the predictor must treat it as missing rather than
    # as a genuinely small story. All three default to unknown so a synthesized
    # story (fix-issue, runlog) and any pre-28.2 caller stay valid unchanged.
    #
    # * ``ac_count``    — acceptance criteria the story states.
    # * ``dep_depth``   — longest dependency chain reaching this story.
    # * ``scope_proxy`` — distinct files/areas the story names (scope proxy).
    ac_count: int | None = None
    dep_depth: int | None = None
    scope_proxy: int | None = None


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
