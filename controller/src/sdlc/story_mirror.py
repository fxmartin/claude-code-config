# ABOUTME: Idempotent story ↔ host-issue mapping — create-once, update-not-duplicate, recover, re-create.
# ABOUTME: Story 22.2-003 — map each story to exactly one issue via the adapter; mapping lives in the inventory.

from __future__ import annotations

from dataclasses import dataclass

from sdlc.build import Ledger
from sdlc.issue_host import Issue, IssueHostAdapter, IssueHostError
from sdlc.story_render import (
    StoryDoc,
    issue_title,
    render_issue_body,
    story_labels,
    story_marker,
)

__all__ = [
    "CREATED",
    "UPDATED",
    "RECOVERED",
    "RECREATED",
    "MirrorOutcome",
    "mirror_story",
    "mirror_stories",
]

# What one mirror pass did to a story's issue. `created`: no prior mapping and no
# issue found by marker — a fresh issue. `updated`: the inventory ref still
# resolves — the existing issue was refreshed. `recovered`: the local ref was
# missing/stale but the issue was re-discovered by its hidden body marker and the
# mapping re-recorded (no duplicate). `recreated`: a previously mapped issue was
# deleted on the host and its marker is gone too — the orphan is re-created rather
# than silently lost (Story 22.2-003 AC3).
CREATED = "created"
UPDATED = "updated"
RECOVERED = "recovered"
RECREATED = "recreated"


@dataclass(frozen=True)
class MirrorOutcome:
    """The result of mirroring one story to its host issue.

    ``ref`` is the issue's `issue_ref` after the pass (the same one on update, a
    new one on create/recreate, the re-discovered one on recover). ``action`` is
    one of the module constants above. ``issue`` is the adapter's returned
    :class:`Issue` for the create/update call.
    """

    story_id: str
    host: str
    ref: str
    action: str
    issue: Issue


def _refresh(adapter: IssueHostAdapter, ref: str, doc: StoryDoc) -> Issue:
    """Push the MD-rendered title/body/labels onto an existing issue (MD wins).

    Writes the freshly rendered managed block; preserving human content *outside*
    the managed region is the reconcile/sync path's concern (Story 22.4-001),
    which fetches the live body first. Here the goal is the mapping, not drift.
    """
    return adapter.issue_update(
        ref,
        title=issue_title(doc),
        body=render_issue_body(doc),
        labels=story_labels(doc.epic, doc.feature, doc.points, doc.risk),
    )


def mirror_story(
    adapter: IssueHostAdapter, ledger: Ledger, doc: StoryDoc
) -> MirrorOutcome:
    """Map one story to exactly one host issue, idempotently.

    The mapping lives in the inventory (`host`+`issue_ref`) and is recoverable
    from the issue body's ``<!-- sdlc-story: <id> -->`` marker via the adapter's
    ``issue_find``. The resolution order (Story 22.2-003 AC):

    1. **By inventory ref** — if the story is mapped *on this host* and the ref
       still resolves, update that issue (``UPDATED``). A ref recorded for a
       *different* host is ignored here (this host is unmapped).
    2. **By marker** — if there is no usable ref, or the ref no longer resolves
       (deleted on the host), re-discover the issue by its marker. Found →
       refresh it and re-record the mapping (``RECOVERED``).
    3. **Create** — nothing maps and nothing is found. A fresh story creates a
       new issue (``CREATED``); an orphaned mapping whose issue *and* marker are
       both gone is re-created (``RECREATED``), never silently dropped.

    Every host call goes through the adapter, so the same logic drives GitHub and
    GitLab. Returns a :class:`MirrorOutcome` describing what happened.
    """
    host = adapter.host
    mapping = ledger.inventory_get_mapping(doc.story_id)
    # Only trust a ref recorded for *this* host; a cross-host ref means this host
    # has no issue yet (the story may have been mirrored elsewhere).
    ref = mapping[1] if mapping and mapping[0] == host else None
    had_ref = ref is not None

    # 1. Update by the known ref.
    if ref is not None:
        try:
            issue = _refresh(adapter, ref, doc)
            return MirrorOutcome(doc.story_id, host, ref, UPDATED, issue)
        except IssueHostError:
            # The ref no longer resolves (issue deleted on the host). Fall
            # through to marker recovery, then re-create — never duplicate while
            # the issue might still exist.
            ref = None

    # 2. Recover by the body marker.
    marker = story_marker(doc.story_id)
    found = adapter.issue_find(marker)
    if found is not None:
        issue = _refresh(adapter, found.ref, doc)
        ledger.inventory_set_mapping(doc.story_id, host, found.ref)
        return MirrorOutcome(doc.story_id, host, found.ref, RECOVERED, issue)

    # 3. Create fresh, or re-create an orphan whose marker is also gone.
    issue = adapter.issue_create(
        title=issue_title(doc),
        body=render_issue_body(doc),
        labels=story_labels(doc.epic, doc.feature, doc.points, doc.risk),
    )
    ledger.inventory_set_mapping(doc.story_id, host, issue.ref)
    action = RECREATED if had_ref else CREATED
    return MirrorOutcome(doc.story_id, host, issue.ref, action, issue)


def mirror_stories(
    adapter: IssueHostAdapter, ledger: Ledger, docs: list[StoryDoc]
) -> list[MirrorOutcome]:
    """Mirror a batch of stories in order, each via :func:`mirror_story`.

    All host calls route through the single adapter so the pass is rate-limit
    aware at one seam; the per-story mapping is persisted as it goes, so an
    interrupted batch resumes cheaply (already-mapped stories update, none
    duplicate). Returns one :class:`MirrorOutcome` per input doc.
    """
    return [mirror_story(adapter, ledger, doc) for doc in docs]
