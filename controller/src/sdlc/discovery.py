# ABOUTME: Reads stories from epic markdown into the build queue (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers + Priority/Points/Dependencies.

from __future__ import annotations

import re
from pathlib import Path

from sdlc.cohort import Story

# `##### Story 7.3-001: Port build-stories ...`
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):\s*(.+?)\s*$")
_PRIORITY = re.compile(r"^\*\*Priority\*\*:\s*(\S+)")
# Accept both `**Points**:` and the `**Story Points**:` form epics actually use.
_POINTS = re.compile(r"^\*\*(?:Story\s+)?Points\*\*:\s*([0-9]+)")
_DEPENDENCIES = re.compile(r"^\*\*Dependencies\*\*:\s*(.+?)\s*$")
# Pull every `X.Y-NNN` story id mentioned in a Dependencies line.
_DEP_ID = re.compile(r"[0-9]+\.[0-9]+-[0-9]+")
# A story is shipped when its **Status**: line starts "Done", or when its
# Definition-of-Done checklist exists and every box is checked.
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(.+?)\s*$")
_DOD_BOX = re.compile(r"^\s*-\s*\[([ xX])\]")
# A bare scope that names exactly one story, e.g. `34.5-003`.
_STORY_ID_SCOPE = re.compile(r"^[0-9]+\.[0-9]+-[0-9]+$")
# The numeric epic id embedded in an `epic-34-*.md` / `epic-07-*.md` filename.
_EPIC_FILE_NUM = re.compile(r"^epic-0*([0-9]+)")

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

    def _is_done(c: dict) -> bool:
        """Shipped when Status starts 'Done', or DoD has boxes and all are checked."""
        status = c.get("status", "")
        if status.strip().lower().startswith("done"):
            return True
        boxes_total = c.get("dod_total", 0)
        return boxes_total > 0 and c.get("dod_checked", 0) == boxes_total

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
                done=_is_done(current),
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
        if box := _DOD_BOX.match(line):
            current["dod_total"] = current.get("dod_total", 0) + 1
            if box.group(1) != " ":
                current["dod_checked"] = current.get("dod_checked", 0) + 1
        elif m := _STATUS.match(line):
            current.setdefault("status", m.group(1))  # first Status line wins
        elif m := _PRIORITY.match(line):
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

    ``scope`` accepts ``all`` (every epic), ``epic-NN`` (one epic by number), a
    bare epic name, or a single story id ``X.Y-NNN`` (resolved to its epic by the
    leading major number, returning only that story). Returns an empty queue when
    nothing matches — the caller decides whether that is an error.
    """
    root = root or Path.cwd()
    story_dir = _story_dir(root)
    if story_dir is None:
        return []

    epic_files = sorted(story_dir.glob("epic-*.md"))

    # Single-story scope: find the epic by major number and return just that story.
    if _STORY_ID_SCOPE.match(scope.strip()):
        target = scope.strip()
        major = int(target.split(".", 1)[0])
        for epic in epic_files:
            m = _EPIC_FILE_NUM.match(epic.stem.lower())
            if m and int(m.group(1)) == major:
                for story in parse_epic_file(epic):
                    if story.id == target:
                        return [story]
        return []

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
