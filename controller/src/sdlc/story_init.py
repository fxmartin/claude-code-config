# ABOUTME: Full-backfill engine for `sdlc issues init` — board+taxonomy for every epic/story.
# ABOUTME: Story 22.3-001 — create an issue per story, close Done ones, resume idempotently.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdlc.build import Ledger
from sdlc.discovery import _story_dir, parse_epic_file
from sdlc.issue_host import IssueHostAdapter
from sdlc.story_inventory import project_specs
from sdlc.story_mirror import MirrorOutcome, mirror_stories
from sdlc.story_render import parse_story_docs

__all__ = [
    "NoStoriesError",
    "InitResult",
    "done_story_ids",
    "init_issues",
]


class NoStoriesError(Exception):
    """No framework-format stories were found — point the user at generate-epics.

    Raised by :func:`init_issues` before any host call so a repo that has not yet
    run ``generate-epics`` gets clear guidance rather than an empty backfill.
    """


@dataclass(frozen=True)
class InitResult:
    """What one ``sdlc issues init`` pass provisioned.

    ``total`` is the number of stories backfilled (one issue each). ``outcomes``
    is the per-story :class:`MirrorOutcome` (created/updated/recovered/recreated).
    ``closed`` lists the ids of Done stories whose issue init closed, so the board
    shows full history while the open-issues list stays = real remaining work.
    """

    host: str
    total: int
    outcomes: list[MirrorOutcome]
    closed: list[str]


def done_story_ids(root: Path | None = None) -> set[str]:
    """The ids of every shipped story across all ``docs/stories/epic-*.md``.

    A story is Done when its ``**Status**:`` line starts "Done" or its
    Definition-of-Done checklist is fully checked — the same git-agnostic rule
    discovery uses to skip already-built work. Returns an empty set when no story
    directory exists.
    """
    story_dir = _story_dir(root or Path.cwd())
    if story_dir is None:
        return set()
    done: set[str] = set()
    for epic_file in sorted(story_dir.glob("epic-*.md")):
        for story in parse_epic_file(epic_file):
            if story.done:
                done.add(story.id)
    return done


def init_issues(
    adapter: IssueHostAdapter, ledger: Ledger, root: Path | None = None
) -> InitResult:
    """Stand up the full board: an issue for **every** story across **every** epic.

    The one command to adopt a repo (Story 22.3-001). The steps:

    1. **Project the specs** into the inventory so a spec row exists for every
       story before the mirror records its mapping onto it.
    2. **Backfill** — mirror every story via the idempotent engine
       (:func:`mirror_stories`): each gets one issue carrying the portable
       taxonomy labels, with its ``host``+``issue_ref`` recorded. A re-run updates
       rather than duplicates, so an interrupted or rate-limited init resumes
       cheaply (Story 22.3-001 AC3).
    3. **Close the Done stories** — a story whose status is Done has its issue
       created *and immediately closed*, so the board shows full history while the
       open-issues list stays = real remaining work (AC2). ``issue_close`` is
       idempotent on both hosts, so a resume re-closes harmlessly.

    Raises :class:`NoStoriesError` (before any host call) when the repo has no
    framework-format stories, pointing the user at ``generate-epics`` (AC4).
    Returns an :class:`InitResult` summarising the pass.
    """
    docs = parse_story_docs(root)
    if not docs:
        raise NoStoriesError(
            "no framework-format stories found under docs/stories/; "
            "run `generate-epics` to author them first"
        )

    # 1. Spec rows must exist before the mirror writes mappings onto them.
    project_specs(ledger, root)

    # 2. Backfill every story — idempotent, so a resume updates not duplicates.
    outcomes = mirror_stories(adapter, ledger, docs)

    # 3. Done stories are created then immediately closed (full board history).
    done = done_story_ids(root)
    closed: list[str] = []
    for outcome in outcomes:
        if outcome.story_id in done:
            adapter.issue_close(outcome.ref)
            closed.append(outcome.story_id)

    return InitResult(
        host=adapter.host, total=len(docs), outcomes=outcomes, closed=closed
    )
