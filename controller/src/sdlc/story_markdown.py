# ABOUTME: Pure renderer for epic-markdown `**Status**: Done` markers (Issue #598;
# ABOUTME: Story 32.1-003). render_story_done/render_done_markers touch no disk;
# ABOUTME: mark_story_done/render_epic_file are the thin on-demand write wrappers.

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

# Mirrors discovery.py's `_STORY_HEADER`, capturing only the story id.
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):")
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(.+?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s")
# The numeric epic id embedded in an `epic-34-*.md` / `epic-07-*.md` filename,
# mirroring discovery.py's `_EPIC_FILE_NUM`.
_EPIC_FILE_NUM = re.compile(r"^epic-0*([0-9]+)")

_STORY_DIR_CANDIDATES = ("docs/stories", "stories")

__all__ = [
    "find_epic_file",
    "mark_story_done",
    "render_done_markers",
    "render_epic_file",
    "render_story_done",
]


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


def render_story_done(text: str, story_id: str) -> tuple[str, bool]:
    """Pure transform: set ``**Status**: Done`` on ``story_id``'s block in ``text``.

    Replaces an existing ``**Status**:`` line's value with ``Done``, or inserts
    one right after the story's header when the story states none — the same
    fast-path :func:`sdlc.discovery.parse_epic_file`'s ``_is_done`` reads.
    Returns ``(text, False)`` unchanged for a no-op (the story id has no
    matching header, or its status already reads "Done"), else the rendered
    text and ``True``. Touches no disk — the on-demand writers
    (:func:`mark_story_done`, :func:`render_epic_file`) are thin wrappers
    around this (Story 32.1-003).
    """
    lines = text.splitlines(keepends=True)

    start: int | None = None
    for i, line in enumerate(lines):
        m = _STORY_HEADER.match(line)
        if m and m.group(1) == story_id:
            start = i
            break
    if start is None:
        return text, False

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _STORY_HEADER.match(lines[i]) or _ANY_HEADING.match(lines[i]):
            end = i
            break

    for i in range(start + 1, end):
        m = _STATUS.match(lines[i])
        if m:
            if m.group(1).strip().lower().startswith("done"):
                return text, False
            newline = "\n" if lines[i].endswith("\n") else ""
            lines[i] = f"**Status**: Done{newline}"
            return "".join(lines), True

    if not lines[start].endswith("\n"):
        lines[start] = lines[start] + "\n"
    lines.insert(start + 1, "**Status**: Done\n")
    return "".join(lines), True


def render_done_markers(text: str, done_story_ids: Iterable[str]) -> tuple[str, list[str]]:
    """Pure batch transform: stamp every id in ``done_story_ids`` found in ``text``.

    Applies :func:`render_story_done` once per id, threading the text through
    each call, and collects the ids that actually changed something (in
    ``done_story_ids`` order) — skipping ids with no matching header or an
    already-"Done" status. Touches no disk.
    """
    changed_ids: list[str] = []
    for story_id in done_story_ids:
        text, changed = render_story_done(text, story_id)
        if changed:
            changed_ids.append(story_id)
    return text, changed_ids


def mark_story_done(epic_file: str | Path, story_id: str) -> bool:
    """Write :func:`render_story_done`'s result to ``epic_file`` on request.

    Returns ``True`` when the file was written, ``False`` for a no-op. Raises
    ``OSError`` (e.g. the epic file does not exist) — the caller's job. This is
    an on-demand writer only (Story 32.1-003): nothing in the build/reconcile
    run path calls it mid-run any more, so the shared checkout stays untouched
    for the run's duration; callers invoke it explicitly (e.g. ``sdlc
    reconcile``) to render the ledger's facts into the checkout.
    """
    path = Path(epic_file)
    text = path.read_text(encoding="utf-8")
    new_text, changed = render_story_done(text, story_id)
    if changed:
        path.write_text(new_text, encoding="utf-8")
    return changed


def render_epic_file(epic_file: str | Path, done_story_ids: Iterable[str]) -> list[str]:
    """Write :func:`render_done_markers`'s result to ``epic_file`` on request.

    The batch counterpart to :func:`mark_story_done`: renders every id in
    ``done_story_ids`` in one read/write pass and returns the ids that actually
    changed. Writes only when at least one id changed something. Raises
    ``OSError`` (e.g. the epic file does not exist) — the caller's job.
    """
    path = Path(epic_file)
    text = path.read_text(encoding="utf-8")
    new_text, changed_ids = render_done_markers(text, done_story_ids)
    if changed_ids:
        path.write_text(new_text, encoding="utf-8")
    return changed_ids
