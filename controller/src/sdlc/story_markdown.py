# ABOUTME: Pure renderer for epic-markdown `**Status**: Done` markers (Issue #598;
# ABOUTME: Story 32.1-003). render_story_done/render_done_markers touch no disk;
# ABOUTME: mark_story_done/render_epic_file are the thin on-demand write wrappers.

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from sdlc.discovery import NFR_FILE_NAME, STORY_ID, canonical_story_id, nfr_epic_feature

# discovery.py's `_STORY_HEADER` over the shared id pattern, capturing only the id.
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+(" + STORY_ID + r"):")
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(.+?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s")
# The numeric epic id embedded in an `epic-34-*.md` / `epic-07-*.md` filename,
# mirroring discovery.py's `_EPIC_FILE_NUM`.
_EPIC_FILE_NUM = re.compile(r"^epic-0*([0-9]+)")

_STORY_DIR_CANDIDATES = ("docs/stories", "stories")

# Story 36.1-001: the NFR file's optional `## NFR Progress` table, whose row for
# a merged NFR story gets its `Status` cell set to Done alongside the status line.
_NFR_PROGRESS_HEADING = re.compile(r"^#{1,6}\s*NFR\s+Progress\s*$", re.IGNORECASE)
_TABLE_SEPARATOR_CELL = re.compile(r"^\s*:?-+:?\s*$")

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
    An ``NFR-<AREA>-NNN`` id resolves to the story dir's NFR file instead
    (Story 36.1-001).
    """
    if nfr_epic_feature(story_id) is not None:
        for candidate in _STORY_DIR_CANDIDATES:
            nfr_file = root / candidate / NFR_FILE_NAME
            if nfr_file.is_file():
                return nfr_file
        return None
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
    Story 36.1-001: also sets the story's ``Status`` cell in a ``## NFR
    Progress`` table, when the text has one. Returns ``(text, False)``
    unchanged for a no-op (the story id has no matching header, or its status
    already reads "Done"), else the rendered text and ``True``. Touches no disk
    — the on-demand writers (:func:`mark_story_done`, :func:`render_epic_file`)
    are thin wrappers around this (Story 32.1-003).
    """
    text, status_changed = _render_status_line(text, story_id)
    text, row_changed = _render_progress_row(text, story_id)
    return text, status_changed or row_changed


def _render_status_line(text: str, story_id: str) -> tuple[str, bool]:
    """Set ``**Status**: Done`` in ``story_id``'s own block (see :func:`render_story_done`)."""
    lines = text.splitlines(keepends=True)
    story_id = canonical_story_id(story_id)

    start: int | None = None
    for i, line in enumerate(lines):
        m = _STORY_HEADER.match(line)
        if m and canonical_story_id(m.group(1)) == story_id:
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


def _render_progress_row(text: str, story_id: str) -> tuple[str, bool]:
    """Set ``story_id``'s ``Status`` cell to Done in a ``## NFR Progress`` table.

    A no-op when there is no such section, its table has no ``Status`` column,
    no row names the story, or the cell already reads Done. Only that one cell
    is rewritten, so the rest of the table is byte-identical.
    """
    id_token = re.compile(
        r"(?<![A-Za-z0-9-])" + re.escape(story_id) + r"(?![A-Za-z0-9-])", re.IGNORECASE
    )
    lines = text.splitlines(keepends=True)
    in_section = False
    status_col: int | None = None  # resolved from the table's header row
    for i, line in enumerate(lines):
        if _ANY_HEADING.match(line):
            in_section = bool(_NFR_PROGRESS_HEADING.match(line))
            status_col = None
            continue
        if not in_section or not line.lstrip().startswith("|"):
            continue
        body = line.rstrip("\r\n")
        # `parts[0]`/`parts[-1]` are what sits outside the outer pipes.
        parts = body.split("|")
        cells = parts[1:-1]
        if status_col is None:
            headers = [c.strip().lower() for c in cells]
            status_col = headers.index("status") if "status" in headers else -1
            continue
        if status_col < 0 or status_col >= len(cells):
            continue
        if all(_TABLE_SEPARATOR_CELL.match(c) for c in cells):
            continue
        if not any(id_token.search(c) for j, c in enumerate(cells) if j != status_col):
            continue
        if cells[status_col].strip().lower().startswith("done"):
            return text, False
        parts[status_col + 1] = " Done "
        lines[i] = "|".join(parts) + line[len(body):]
        return "".join(lines), True
    return text, False


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
