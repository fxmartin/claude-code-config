# ABOUTME: Project the MD story specs into the `story_inventory` ledger cache.
# ABOUTME: Story 22.1-002 — parse every epic-*.md story, upsert idempotently, flag removed.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sdlc.build import Ledger
from sdlc.discovery import _STORY_HEADER, _story_dir

__all__ = ["StorySpec", "ProjectionResult", "parse_inventory_specs", "project_specs"]

# `**Story Points**: 5` / `**Points**: 5` — same form discovery.py accepts.
_POINTS = re.compile(r"^\*\*(?:Story\s+)?Points\*\*:\s*([0-9]+)")
# `**Risk Level**: Medium` — capture the first word only; a trailing
# ` — prose` aside (several epics carry one) is discarded.
_RISK = re.compile(r"^\*\*Risk Level\*\*:\s*([A-Za-z]+)")


@dataclass(frozen=True)
class StorySpec:
    """One story's spec, projected from the MD (the projector's spec-only view).

    ``epic`` and ``feature`` are derived from the id: ``22.1-002`` → epic ``22``,
    feature ``22.1``. ``points``/``risk`` are ``None`` when the MD omits them, so
    a malformed story still projects rather than being dropped.
    """

    story_id: str
    epic: str
    feature: str
    title: str
    points: int | None
    risk: str | None


@dataclass(frozen=True)
class ProjectionResult:
    """What one projection pass changed, for the caller to report/act on.

    ``removed`` lists ids present in the inventory but no longer in the MD — they
    are *flagged here, not deleted* (a row already linked to a host issue must not
    vanish silently). ``total`` is the number of stories the MD currently holds.
    """

    added: list[str]
    updated: list[str]
    removed: list[str]
    total: int


def _feature_and_epic(story_id: str) -> tuple[str, str]:
    """Derive ``(epic, feature)`` from a story id, e.g. ``22.1-002`` → ``('22', '22.1')``."""
    feature = story_id.split("-", 1)[0]
    epic = feature.split(".", 1)[0]
    return epic, feature


def _parse_epic_file(path: Path) -> list[StorySpec]:
    """Parse one epic markdown file into its story specs.

    Walks the file line by line: a ``##### Story N.F-NNN: title`` header opens a
    story block; the first ``Story Points`` and ``Risk Level`` lines inside that
    block populate it (later duplicates are ignored, matching the
    first-line-wins discipline in discovery.py).
    """
    specs: list[StorySpec] = []
    current: dict | None = None

    def _flush() -> None:
        if current is None:
            return
        epic, feature = _feature_and_epic(current["id"])
        specs.append(
            StorySpec(
                story_id=current["id"],
                epic=epic,
                feature=feature,
                title=current["title"],
                points=current.get("points"),
                risk=current.get("risk"),
            )
        )

    for line in path.read_text(encoding="utf-8").splitlines():
        header = _STORY_HEADER.match(line)
        if header:
            _flush()
            current = {"id": header.group(1), "title": header.group(2)}
            continue
        if current is None:
            continue
        if "points" not in current and (m := _POINTS.match(line)):
            current["points"] = int(m.group(1))
        elif "risk" not in current and (m := _RISK.match(line)):
            current["risk"] = m.group(1)

    _flush()
    return specs


def parse_inventory_specs(root: Path | None = None) -> list[StorySpec]:
    """Parse every story across all ``docs/stories/epic-*.md`` into specs.

    The epic files are the authoritative per-story source (``STORIES.md`` is the
    epic-level index and carries no per-story spec rows). Returns an empty list
    when no story directory exists — the caller decides whether that is an error.
    Files are read in sorted order so the projection is deterministic.
    """
    story_dir = _story_dir(root or Path.cwd())
    if story_dir is None:
        return []
    specs: list[StorySpec] = []
    for epic_file in sorted(story_dir.glob("epic-*.md")):
        specs.extend(_parse_epic_file(epic_file))
    return specs


def project_specs(ledger: Ledger, root: Path | None = None) -> ProjectionResult:
    """Project the MD story specs into the ledger's ``story_inventory`` cache.

    Idempotent: existing stories are updated in place (spec columns only — the
    sync/build-owned cache columns are preserved), new stories are inserted, and
    stories that disappeared from the MD are reported as ``removed`` without being
    deleted. Returns a :class:`ProjectionResult` describing the change set.
    """
    specs = parse_inventory_specs(root)
    seen = {s.story_id for s in specs}
    before = ledger.inventory_story_ids()

    added = sorted(seen - before)
    updated = sorted(seen & before)
    removed = sorted(before - seen)

    ledger.inventory_upsert_specs(
        (s.story_id, s.epic, s.feature, s.title, s.points, s.risk) for s in specs
    )

    return ProjectionResult(
        added=added, updated=updated, removed=removed, total=len(specs)
    )
