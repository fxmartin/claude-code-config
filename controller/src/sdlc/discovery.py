# ABOUTME: Reads stories from epic markdown into the build queue (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers + Priority/Points/Dependencies.

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from sdlc.cohort import Story

# `##### Story 7.3-001: Port build-stories ...`
_STORY_HEADER = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):\s*(.+?)\s*$")
_PRIORITY = re.compile(r"^\*\*Priority\*\*:\s*(\S+)")
# Accept both `**Points**:` and the `**Story Points**:` form epics actually use.
_POINTS = re.compile(r"^\*\*(?:Story\s+)?Points\*\*:\s*([0-9]+)")
_DEPENDENCIES = re.compile(r"^\*\*Dependencies\*\*:\s*(.+?)\s*$")
# Pull every `X.Y-NNN` story id mentioned in a Dependencies line. Edge
# extraction is constrained to the *leading head* of the line — see
# `_dependency_head` / `_parse_dependency_edges` — so prose-mentioned ids do not
# become phantom edges (Story 12.5-001).
_DEP_ID = re.compile(r"[0-9]+\.[0-9]+-[0-9]+")
# At parenthesis-depth 0 these begin explanatory prose, ending the edge list.
_DEP_PROSE_DELIMS = (";", "—", "–")
# A leading one of these words means "no dependencies".
_DEP_NONE_MARKERS = {"none", "n/a", "na", "tbd"}
# A story is shipped when its **Status**: line starts "Done", or when its
# Definition-of-Done checklist exists and every box is checked.
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(.+?)\s*$")
_DOD_BOX = re.compile(r"^\s*-\s*\[([ xX])\]")
# A bare scope that names exactly one story, e.g. `34.5-003`.
_STORY_ID_SCOPE = re.compile(r"^[0-9]+\.[0-9]+-[0-9]+$")
# Scope tokens are separated by commas and/or whitespace (`epic-15,epic-18` or
# `epic-15 epic-18`) so both forms a user can type resolve identically.
_SCOPE_SEP = re.compile(r"[,\s]+")
# The numeric epic id embedded in an `epic-34-*.md` / `epic-07-*.md` filename.
_EPIC_FILE_NUM = re.compile(r"^epic-0*([0-9]+)")
# Any markdown heading. A non-story heading (feature/epic level, e.g.
# `### Feature 27.3` or `## Verification`) ends the current story's *section*
# capture (Story 27.3-002) while metadata parsing continues unchanged.
_ANY_HEADING = re.compile(r"^#{1,6}\s")
# A fenced-code-block delimiter. Lines inside a fence never end section
# capture — a spec's ```markdown/```bash example can open with `# Changelog`
# or a shell comment, which would otherwise read as a heading and silently
# truncate the embedded spec mid-block (the exact failure AC2's "no truncated
# specs ever injected" forbids; regression: epic-05 story 5.3-001).
_CODE_FENCE = re.compile(r"^\s*(```|~~~)")

_STORY_DIR_CANDIDATES = ("docs/stories", "stories")


def _dependency_head(content: str) -> str:
    """Return the leading edge-list segment of a ``**Dependencies**:`` value.

    Walks ``content`` collecting only text at parenthesis-depth 0 and stops at
    the first depth-0 prose delimiter (``;`` / em- or en-dash) or sentence-ending
    period (a ``.`` followed by whitespace or end-of-string). Parenthetical asides
    — and any story ids inside them — are skipped, so only the intended leading
    edges survive. The ``.`` inside an id (``12.3-001``) is never a delimiter
    because it is followed by a digit, not whitespace.
    """
    head: list[str] = []
    depth = 0
    n = len(content)
    for i, ch in enumerate(content):
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
            continue
        if depth > 0:
            continue
        if ch in _DEP_PROSE_DELIMS:
            break
        if ch == "." and (i + 1 >= n or content[i + 1].isspace()):
            break
        head.append(ch)
    return "".join(head).strip()


def _parse_dependency_edges(content: str, self_id: str) -> list[str]:
    """Extract the intended dependency edge ids from a Dependencies value.

    Only the leading run of bare ``X.Y-NNN`` ids is read (see
    :func:`_dependency_head`); ids that appear only in parenthetical or sentence
    prose are ignored, and a leading ``None`` / ``N/A`` / ``TBD`` yields no edges.
    Self-references are dropped defensively. This is the root-cause fix for a
    benignly-worded story line creating a phantom dependency cycle that crashes
    cohort scheduling (Story 12.5-001).
    """
    head = _dependency_head(content)
    if not head:
        return []
    first_word = head.split(None, 1)[0].rstrip(".").lower()
    if first_word in _DEP_NONE_MARKERS:
        return []
    return [d for d in _DEP_ID.findall(head) if d != self_id]


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
    ignored — only intra-project ``X.Y-NNN`` edges feed cohort scheduling. Only
    the leading edge list of a ``Dependencies`` line is read (story ids in
    parenthetical/sentence prose are ignored) so explanatory prose cannot create
    a phantom dependency cycle — see :func:`_parse_dependency_edges`.
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
                # Trailing blank lines are noise between stories, not spec.
                section="\n".join(current["section_lines"]).rstrip(),
            )
        )

    for line in text.splitlines():
        header = _STORY_HEADER.match(line)
        if header:
            _flush()
            current = {
                "id": header.group(1),
                "title": header.group(2),
                "section_lines": [line],
            }
            continue
        if current is None:
            continue
        # Story 27.3-002: capture the section verbatim until the next heading
        # (the next story, or a feature/epic-level heading ending this story's
        # block). Heading-looking lines inside a code fence are spec content,
        # not structure. Metadata parsing below is deliberately untouched.
        if _CODE_FENCE.match(line):
            current["in_fence"] = not current.get("in_fence", False)
        if _ANY_HEADING.match(line) and not current.get("in_fence"):
            current["section_done"] = True
        elif not current.get("section_done"):
            current["section_lines"].append(line)
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
            current["dependencies"] = _parse_dependency_edges(m.group(1), current["id"])

    _flush()
    return stories


def _story_dir(root: Path) -> Path | None:
    for candidate in _STORY_DIR_CANDIDATES:
        path = root / candidate
        if path.is_dir():
            return path
    return None


def _split_scope(scope: str) -> list[str]:
    """Split a (possibly composite) scope string into its individual tokens.

    Tokens may be comma- and/or whitespace-separated; empty fragments are
    dropped. ``"epic-15,epic-18"`` and ``"epic-15 epic-18"`` both yield
    ``["epic-15", "epic-18"]``.
    """
    return [t for t in _SCOPE_SEP.split(scope.strip()) if t]


def canonical_scope(scope: str | Iterable[str]) -> str:
    """Normalise one or more scope tokens into a single canonical label.

    Accepts a raw string (which may itself be comma-/space-separated) or an
    iterable of tokens, and returns a lowercased, deduped, sorted, comma-joined
    label — the form persisted in the ledger/registry and matched on resume so a
    composite run resolves regardless of the order its scopes were typed (Story
    19.1-001). ``all`` (alone or mixed with explicit epics) collapses to ``all``
    since it already covers every epic; an empty input defaults to ``all``.
    """
    raw: list[str] = [scope] if isinstance(scope, str) else list(scope)
    tokens = [t.lower() for item in raw for t in _split_scope(item)]
    if not tokens:
        return "all"
    if "all" in tokens:
        return "all"
    return ",".join(sorted(set(tokens)))


def _resolve_scope_token(token: str, epic_files: list[Path]) -> list[Story]:
    """Resolve a single scope token to its stories (the pre-19.1-001 behaviour).

    ``token`` is ``all`` (every epic), ``epic-NN`` (one epic by number), a bare
    epic name substring, or a single story id ``X.Y-NNN`` (resolved to its epic
    by the leading major number, returning only that story).
    """
    token = token.strip()

    # Single-story scope: find the epic by major number and return just that story.
    if _STORY_ID_SCOPE.match(token):
        major = int(token.split(".", 1)[0])
        for epic in epic_files:
            m = _EPIC_FILE_NUM.match(epic.stem.lower())
            if m and int(m.group(1)) == major:
                for story in parse_epic_file(epic):
                    if story.id == token:
                        return [story]
        return []

    selected: list[Path] = []
    token_l = token.lower()
    for epic in epic_files:
        if token_l == "all":
            selected.append(epic)
        elif token_l.startswith("epic-"):
            # Match epic-07 against epic-07-external-controller.md.
            number = token_l.split("-", 1)[1]
            if epic.stem.lower().startswith(f"epic-{number}"):
                selected.append(epic)
        elif token_l and token_l in epic.stem.lower():
            selected.append(epic)

    stories: list[Story] = []
    for epic in selected:
        stories.extend(parse_epic_file(epic))
    return stories


def discover_queue(scope: str, root: Path | None = None) -> list[Story]:
    """Build the story queue for ``scope`` from the markdown epic files.

    ``scope`` accepts one *or several* tokens (comma-/space-separated): ``all``
    (every epic), ``epic-NN`` (one epic by number), a bare epic name, or a single
    story id ``X.Y-NNN`` (resolved to its epic by the leading major number,
    returning only that story). Multiple tokens are resolved independently and
    unioned, deduped by ``Story.id`` with first-seen order preserved (Story
    19.1-001); ``all`` anywhere in the list resolves to every epic. Returns an
    empty queue when nothing matches — the caller decides whether that is an error.
    """
    root = root or Path.cwd()
    story_dir = _story_dir(root)
    if story_dir is None:
        return []

    epic_files = sorted(story_dir.glob("epic-*.md"))

    tokens = _split_scope(scope)
    if any(t.lower() == "all" for t in tokens):
        tokens = ["all"]

    queue: list[Story] = []
    seen: set[str] = set()
    for token in tokens:
        for story in _resolve_scope_token(token, epic_files):
            if story.id not in seen:
                seen.add(story.id)
                queue.append(story)
    return queue
