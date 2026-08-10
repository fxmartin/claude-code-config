# ABOUTME: Write-back counterpart to discovery.parse_epic_file's read (Issue #598).
# ABOUTME: Stamps `**Status**: Done` onto a story's block once it actually lands.

from __future__ import annotations

import re
from pathlib import Path

# Mirrors discovery.py's `_STORY_HEADER`, capturing only the story id.
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):")
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(.+?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s")
# The numeric epic id embedded in an `epic-34-*.md` / `epic-07-*.md` filename,
# mirroring discovery.py's `_EPIC_FILE_NUM`.
_EPIC_FILE_NUM = re.compile(r"^epic-0*([0-9]+)")

_STORY_DIR_CANDIDATES = ("docs/stories", "stories")

__all__ = ["find_epic_file", "mark_story_done"]


def find_epic_file(story_id: str, root: Path) -> Path | None:
    """The epic markdown file that owns ``story_id``, or None when not found.

    Resolves by the story id's leading major number (``7.3-001`` -> epic ``7``)
    against every ``docs/stories/epic-*.md`` (or ``stories/epic-*.md``) file's
    name — the same convention :func:`sdlc.discovery.parse_epic_file` files
    under. Used by callers (e.g. reconcile) whose ledger row does not carry the
    ``epic_file`` a discovery-sourced :class:`~sdlc.cohort.Story` already has.
    """
    major = story_id.split(".", 1)[0]
    if not major.isdigit():
        return None
    major_num = int(major)
    for candidate in _STORY_DIR_CANDIDATES:
        story_dir = root / candidate
        if not story_dir.is_dir():
            continue
        for epic_file in sorted(story_dir.glob("epic-*.md")):
            m = _EPIC_FILE_NUM.match(epic_file.stem.lower())
            if m and int(m.group(1)) == major_num:
                return epic_file
    return None


def mark_story_done(epic_file: str | Path, story_id: str) -> bool:
    """Set ``**Status**: Done`` on ``story_id``'s block within ``epic_file``.

    Replaces an existing ``**Status**:`` line's value with ``Done``, or inserts
    one right after the story's header when the story states none — the same
    fast-path :func:`sdlc.discovery.parse_epic_file`'s ``_is_done`` reads.
    Returns ``True`` when the file was written, ``False`` for a no-op (the
    story id has no matching header, or its status already reads "Done").

    Raises ``OSError`` (e.g. the epic file does not exist) — the caller's job,
    since both call sites (``build.py::_record_merge_landing`` and
    ``reconcile.py::reconcile_run``) treat this as best-effort and must
    log-and-continue rather than fail an otherwise-good run.
    """
    path = Path(epic_file)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    start: int | None = None
    for i, line in enumerate(lines):
        m = _STORY_HEADER.match(line)
        if m and m.group(1) == story_id:
            start = i
            break
    if start is None:
        return False

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _STORY_HEADER.match(lines[i]) or _ANY_HEADING.match(lines[i]):
            end = i
            break

    for i in range(start + 1, end):
        m = _STATUS.match(lines[i])
        if m:
            if m.group(1).strip().lower().startswith("done"):
                return False
            newline = "\n" if lines[i].endswith("\n") else ""
            lines[i] = f"**Status**: Done{newline}"
            path.write_text("".join(lines), encoding="utf-8")
            return True

    if not lines[start].endswith("\n"):
        lines[start] = lines[start] + "\n"
    lines.insert(start + 1, "**Status**: Done\n")
    path.write_text("".join(lines), encoding="utf-8")
    return True
