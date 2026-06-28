# ABOUTME: Render a story's host issue (managed, MD-owned body block + taxonomy) host-aware.
# ABOUTME: Story 22.2-002 — pure inventory/MD → markdown; labels + per-host status surface.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sdlc.discovery import _STORY_HEADER, _story_dir
from sdlc.issue_host import GITHUB, GITLAB, SUPPORTED_HOSTS, IssueHostError

__all__ = [
    "MANAGED_OPEN",
    "MANAGED_CLOSE",
    "STORY_LABEL",
    "StoryDoc",
    "StatusSurface",
    "story_marker",
    "issue_title",
    "render_managed_block",
    "render_issue_body",
    "extract_managed_block",
    "replace_managed_block",
    "story_labels",
    "status_surface",
    "parse_story_docs",
]

# The managed region: everything between these markers is MD-owned and is
# regenerated on every sync (MD wins). Human content *outside* the region —
# comments, discussion — is never touched. Kept verbatim so a host that
# normalises whitespace still round-trips the markers.
MANAGED_OPEN = "<!-- managed: do not edit -->"
MANAGED_CLOSE = "<!-- /managed -->"

# The coarse, human-facing filter marking an issue as a framework-managed story
# (distinct from `bug`/`create-issue` issues and humans' own issues). It is the
# fast `sdlc issues sync --label story` filter — *not* the source of identity;
# the hidden per-story marker below is the exact-id match.
STORY_LABEL = "story"

# GitHub Projects v2 surfaces (Story 22.2-002 AC): a native Status field and a
# custom number field `Points` for velocity/roll-up. GitLab Free has neither, so
# its only points surface stays the portable `points:N` label.
_GITHUB_STATUS_FIELD = "Status"
_GITHUB_POINTS_FIELD = "Points"

# `**Story Points**: 5` / `**Points**: 5` — same form discovery.py accepts.
_POINTS = re.compile(r"^\*\*(?:Story\s+)?Points\*\*:\s*([0-9]+)")
# `**Risk Level**: Medium` — first word only; a ` — prose` aside is discarded.
_RISK = re.compile(r"^\*\*Risk Level\*\*:\s*([A-Za-z]+)")


@dataclass(frozen=True)
class StoryDoc:
    """One story's full render input — spec fields plus the verbatim MD body.

    ``epic``/``feature`` are derived from the id (``22.2-002`` → ``22`` / ``22.2``).
    ``spec_md`` is the story block exactly as it appears in the epic file (the
    ``##### Story`` header line excluded, since that becomes the issue *title*),
    so the managed block reproduces the spec without re-formatting it.
    """

    story_id: str
    epic: str
    feature: str
    title: str
    points: int | None
    risk: str | None
    spec_md: str


@dataclass(frozen=True)
class StatusSurface:
    """The host-specific board/status surface for a story issue.

    ``labels`` is the portable taxonomy (the same on both hosts). The remaining
    fields differ by host: GitHub uses a Projects v2 ``Status`` field and a
    custom number field ``Points``; GitLab Free has neither, mapping epics to a
    ``milestone`` shown on an Issue Board (points stay the ``points:N`` label).
    """

    host: str
    labels: tuple[str, ...]
    status_field: str | None
    points_field: tuple[str, int] | None
    milestone: str | None


def story_marker(story_id: str) -> str:
    """The hidden ``<!-- sdlc-story: <id> -->`` marker — the exact-id identity.

    This is the source of identity for a managed issue (the `story` label is only
    the coarse human/list filter and never replaces it). Embedded inside the
    managed block so it survives a host that strips trailing content.
    """
    return f"<!-- sdlc-story: {story_id} -->"


def issue_title(doc: StoryDoc) -> str:
    """The issue title — the story id prefixing its human title (e.g. ``22.2-002: …``)."""
    return f"{doc.story_id}: {doc.title}"


def render_managed_block(doc: StoryDoc) -> str:
    """Render the managed region: open marker, hidden id marker, spec, close marker.

    Pure: ``StoryDoc`` in, markdown out. The body is host-neutral — only the
    status/board *surface* (labels/fields) differs per host, not the prose.
    """
    return "\n".join(
        (
            MANAGED_OPEN,
            story_marker(doc.story_id),
            "",
            doc.spec_md.strip(),
            "",
            MANAGED_CLOSE,
        )
    )


def render_issue_body(doc: StoryDoc) -> str:
    """Render a fresh issue body — currently just the managed block.

    A *fresh* issue is fully managed; once a human adds discussion, use
    :func:`replace_managed_block` to refresh only the managed region.
    """
    return render_managed_block(doc)


# Match a managed region, capturing its inner content. DOTALL so the spec's
# newlines are included; non-greedy so only the first region is taken.
_MANAGED_RE = re.compile(
    re.escape(MANAGED_OPEN) + r"\n?(.*?)\n?" + re.escape(MANAGED_CLOSE),
    re.DOTALL,
)


def extract_managed_block(body: str) -> str | None:
    """Return the inner content of the managed region, or None if absent."""
    m = _MANAGED_RE.search(body)
    return m.group(1) if m else None


def replace_managed_block(existing_body: str, doc: StoryDoc) -> str:
    """Regenerate the managed region from ``doc`` (MD wins), preserving the rest.

    If ``existing_body`` has a managed region it is replaced in place (a hand-edit
    inside it is reverted); content outside the markers — human comments,
    discussion — is left untouched. If there is no managed region one is appended,
    so a human-created issue can be adopted without losing its existing prose.
    """
    block = render_managed_block(doc)
    if _MANAGED_RE.search(existing_body):
        # re.sub would interpret backslashes/group refs in `block`; pass a
        # replacement function so the spec text is inserted literally.
        return _MANAGED_RE.sub(lambda _m: block, existing_body, count=1)
    prefix = existing_body.rstrip()
    return f"{prefix}\n\n{block}" if prefix else block


def story_labels(
    epic: str, feature: str, points: int | None, risk: str | None
) -> list[str]:
    """The taxonomy labels for a story issue (the portable cross-host baseline).

    Always: the `story` marker label, ``epic:NN`` and ``feature:NN.F``. Adds
    ``points:N`` and ``risk:*`` only when known, so a malformed/unpointed story
    still carries the structural labels rather than ``points:None``.
    """
    labels = [STORY_LABEL, f"epic:{epic}", f"feature:{feature}"]
    if points is not None:
        labels.append(f"points:{points}")
    if risk:
        labels.append(f"risk:{risk.lower()}")
    return labels


def status_surface(
    host: str, epic: str, feature: str, points: int | None, risk: str | None
) -> StatusSurface:
    """Build the host-aware board/status surface for a story.

    GitHub gets a Projects v2 ``Status`` field and a ``Points`` number field;
    GitLab Free gets neither and maps the epic to an ``epic-NN`` milestone shown
    on an Issue Board (points remain the ``points:N`` label). The taxonomy labels
    are identical on both. An unsupported host fails fast.
    """
    host = (host or "").lower()
    if host not in SUPPORTED_HOSTS:
        raise IssueHostError(
            f"unsupported host {host!r}; supported hosts: {', '.join(SUPPORTED_HOSTS)}"
        )
    labels = tuple(story_labels(epic, feature, points, risk))
    if host == GITHUB:
        points_field = (_GITHUB_POINTS_FIELD, points) if points is not None else None
        return StatusSurface(
            host=GITHUB,
            labels=labels,
            status_field=_GITHUB_STATUS_FIELD,
            points_field=points_field,
            milestone=None,
        )
    # GITLAB
    return StatusSurface(
        host=GITLAB,
        labels=labels,
        status_field=None,
        points_field=None,
        milestone=f"epic-{epic}",
    )


def _feature_and_epic(story_id: str) -> tuple[str, str]:
    """Derive ``(epic, feature)`` from a story id (``22.2-002`` → ``('22', '22.2')``)."""
    feature = story_id.split("-", 1)[0]
    epic = feature.split(".", 1)[0]
    return epic, feature


def _parse_epic_file(path: Path) -> list[StoryDoc]:
    """Parse one epic file into rich :class:`StoryDoc` records.

    Each ``##### Story N.F-NNN: title`` header opens a block; every line up to the
    next header is captured verbatim as ``spec_md`` (the header itself excluded —
    it becomes the issue title). ``points``/``risk`` are read first-line-wins from
    that body, matching discovery.py / the inventory projector.
    """
    docs: list[StoryDoc] = []
    current: dict | None = None
    lines: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        epic, feature = _feature_and_epic(current["id"])
        docs.append(
            StoryDoc(
                story_id=current["id"],
                epic=epic,
                feature=feature,
                title=current["title"],
                points=current.get("points"),
                risk=current.get("risk"),
                spec_md="\n".join(lines).strip(),
            )
        )

    for line in path.read_text(encoding="utf-8").splitlines():
        header = _STORY_HEADER.match(line)
        if header:
            _flush()
            current = {"id": header.group(1), "title": header.group(2)}
            lines = []
            continue
        if current is None:
            continue
        lines.append(line)
        if "points" not in current and (m := _POINTS.match(line)):
            current["points"] = int(m.group(1))
        elif "risk" not in current and (m := _RISK.match(line)):
            current["risk"] = m.group(1)

    _flush()
    return docs


def parse_story_docs(root: Path | None = None) -> list[StoryDoc]:
    """Parse every story across all ``docs/stories/epic-*.md`` into render docs.

    Returns an empty list when no story directory exists. Files are read in sorted
    order so the projection is deterministic.
    """
    story_dir = _story_dir(root or Path.cwd())
    if story_dir is None:
        return []
    docs: list[StoryDoc] = []
    for epic_file in sorted(story_dir.glob("epic-*.md")):
        docs.extend(_parse_epic_file(epic_file))
    return docs
