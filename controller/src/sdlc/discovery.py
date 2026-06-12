# ABOUTME: Reads stories from epic markdown into the build queue (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers + Priority/Points/Dependencies.

from __future__ import annotations

import re
from pathlib import Path

from sdlc.cohort import Story

# `##### Story 7.3-001: Port build-stories ...`
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):\s*(.+?)\s*$")
_PRIORITY = re.compile(r"^\*\*Priority\*\*:\s*(\S+)")
_POINTS = re.compile(r"^\*\*Points\*\*:\s*([0-9]+)")
_DEPENDENCIES = re.compile(r"^\*\*Dependencies\*\*:\s*(.+?)\s*$")
# Pull every `X.Y-NNN` story id mentioned in a Dependencies line.
_DEP_ID = re.compile(r"[0-9]+\.[0-9]+-[0-9]+")

_STORY_DIR_CANDIDATES = ("docs/stories", "stories")


def _epic_id_from_story(story_id: str) -> str:
    """`7.3-001` → `07` style epic id (the leading major number, zero-padded)."""
    major = story_id.split(".", 1)[0]
    return major.zfill(2)


def _epic_name(epic_path: Path) -> str:
    """`epic-07-external-controller.md` → `external-controller`."""
    stem = epic_path.stem  # epic-07-external-controller
    parts = stem.split("-")
    # Drop the leading 'epic' and numeric token when present.
    if parts and parts[0] == "epic":
        parts = parts[1:]
    if parts and parts[0].isdigit():
        parts = parts[1:]
    return "-".join(parts) if parts else stem


def parse_epic_file(epic_path: Path) -> list[Story]:
    """Parse an epic markdown file into a list of :class:`Story` records.

    Reads each ``##### Story X.Y-NNN: Title`` header and the ``Priority``,
    ``Points``, and ``Dependencies`` lines that follow it (before the next
    story header). Dependencies that are not story ids (e.g. "Epic-04") are
    ignored — only intra-project ``X.Y-NNN`` edges feed cohort scheduling.
    """
    text = epic_path.read_text(encoding="utf-8")
    epic_name = _epic_name(epic_path)
    stories: list[Story] = []

    current: dict | None = None

    def _flush() -> None:
        if current is None:
            return
        sid = current["id"]
        stories.append(
            Story(
                id=sid,
                title=current["title"],
                epic_id=_epic_id_from_story(sid),
                epic_name=epic_name,
                epic_file=str(epic_path),
                priority=current.get("priority", "P2"),
                points=current.get("points", 0),
                agent_type=current.get("agent_type", "general-purpose"),
                dependencies=current.get("dependencies", []),
            )
        )

    for line in text.splitlines():
        header = _STORY_HEADER.match(line)
        if header:
            _flush()
            current = {"id": header.group(1), "title": header.group(2)}
            continue
        if current is None:
            continue
        if m := _PRIORITY.match(line):
            current["priority"] = m.group(1)
        elif m := _POINTS.match(line):
            current["points"] = int(m.group(1))
        elif m := _DEPENDENCIES.match(line):
            deps = _DEP_ID.findall(m.group(1))
            # Exclude self-references defensively.
            current["dependencies"] = [d for d in deps if d != current["id"]]

    _flush()
    return stories


def _story_dir(root: Path) -> Path | None:
    for candidate in _STORY_DIR_CANDIDATES:
        path = root / candidate
        if path.is_dir():
            return path
    return None


def discover_queue(scope: str, root: Path | None = None) -> list[Story]:
    """Build the story queue for ``scope`` from the markdown epic files.

    ``scope`` accepts ``all`` (every epic), ``epic-NN`` (one epic by number), or
    a bare epic name. Returns an empty queue when nothing matches — the caller
    decides whether that is an error.
    """
    root = root or Path.cwd()
    story_dir = _story_dir(root)
    if story_dir is None:
        return []

    epic_files = sorted(story_dir.glob("epic-*.md"))
    selected: list[Path] = []
    scope_l = scope.lower()
    for epic in epic_files:
        if scope_l == "all":
            selected.append(epic)
        elif scope_l.startswith("epic-"):
            # Match epic-07 against epic-07-external-controller.md.
            number = scope_l.split("-", 1)[1]
            if epic.stem.lower().startswith(f"epic-{number}"):
                selected.append(epic)
        elif scope_l in epic.stem.lower():
            selected.append(epic)

    queue: list[Story] = []
    for epic in selected:
        queue.extend(parse_epic_file(epic))
    return queue
