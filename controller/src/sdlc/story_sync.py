# ABOUTME: Field-directional reconcile — push the MD-managed block + status, pull human signals.
# ABOUTME: Story 22.4-001 — strictly one writer per field, so repeated syncs are no-ops (no echo loop).

from __future__ import annotations

from dataclasses import dataclass

from sdlc.build import Ledger
from sdlc.issue_host import IssueHostAdapter
from sdlc.story_render import (
    StoryDoc,
    replace_managed_block,
    story_labels,
)

__all__ = [
    "PUSHED",
    "NOOP",
    "UNMAPPED",
    "HUMAN_STATUS_LABELS",
    "WONTFIX",
    "SyncOutcome",
    "status_label",
    "human_status_from_labels",
    "skip_for_human_status",
    "sync_story",
    "sync_stories",
]

# What one reconcile pass did to a story's issue, from the *push* side only —
# pull never writes to the host, so it does not change the action. `pushed`: the
# managed block and/or missing labels were written (MD/ledger → host).
# `noop`: nothing needed pushing (the managed block was already current and every
# desired label already present) — this is the steady state that proves there is
# no echo loop. `unmapped`: the story has no issue on this host yet (the mirror,
# Story 22.2-003, must run first); the pass is skipped, not an error.
PUSHED = "pushed"
NOOP = "noop"
UNMAPPED = "unmapped"

# The human signal labels the reconcile *pulls* from the host into the inventory
# (`human_status`). A human adds these by hand; the framework never writes them
# (one writer per field). `wontfix` additionally tells the build to skip the
# story; `blocked` is surfaced but does not skip.
WONTFIX = "wontfix"
BLOCKED = "blocked"
# Ordered by precedence: a `wontfix` (work declined) outranks a `blocked` signal.
HUMAN_STATUS_LABELS: tuple[str, ...] = (WONTFIX, BLOCKED)


@dataclass(frozen=True)
class SyncOutcome:
    """The result of reconciling one story with its host issue.

    ``action`` is the push-side verdict (one of the module constants). ``owner``
    and ``human_status`` are the *pulled* host values now cached in the inventory
    (``None`` when the issue has no assignee / no human label). ``ref`` is the
    story's `issue_ref` on this host, or ``None`` when it was unmapped.
    """

    story_id: str
    host: str
    ref: str | None
    action: str
    owner: str | None = None
    human_status: str | None = None


def status_label(status: str | None) -> str | None:
    """Render a cached execution status as a portable ``status:<slug>`` label.

    ``None``/empty → ``None`` (no status surface to push). The slug is lower-cased
    with spaces collapsed to hyphens, so ``"In Progress"`` → ``status:in-progress``
    — a label form valid on both hosts.
    """
    if not status:
        return None
    slug = "-".join(status.strip().split()).lower()
    return f"status:{slug}" if slug else None


def human_status_from_labels(labels: tuple[str, ...]) -> str | None:
    """Pick the human signal (`wontfix`/`blocked`) off a host issue's labels.

    Returns the highest-precedence match (``wontfix`` over ``blocked``), or
    ``None`` when neither is present. Only these framework-recognised human
    labels are pulled; every other label is ignored.
    """
    present = set(labels)
    for label in HUMAN_STATUS_LABELS:
        if label in present:
            return label
    return None


def skip_for_human_status(human_status: str | None) -> bool:
    """Whether the build should skip a story given its pulled human signal.

    Only ``wontfix`` (work a human has explicitly declined) is a skip; ``blocked``
    is surfaced but still worked, and ``None`` never skips.
    """
    return human_status == WONTFIX


def sync_story(
    adapter: IssueHostAdapter, ledger: Ledger, doc: StoryDoc
) -> SyncOutcome:
    """Reconcile one story with its host issue — push managed fields, pull human ones.

    Strictly **field-directional** (Story 22.4-001), which is what prevents drift:

    - **Push** (MD/ledger → host): regenerate the managed spec block in place
      (MD wins; human content *outside* the block is preserved) and add any
      missing taxonomy/status labels. Nothing else on the issue is touched.
    - **Pull** (host → ledger): cache the issue's single assignee as ``owner`` and
      any ``blocked``/``wontfix`` label as ``human_status``. Pull is the *only*
      write-back into the ledger from the host.

    Because push writes only managed fields and pull reads only human fields, a
    second pass with no real change writes nothing to the host (``NOOP``) — there
    is no echo loop. A story not yet mapped on this host is skipped (``UNMAPPED``);
    the mirror (Story 22.2-003) must create the issue first.
    """
    host = adapter.host
    mapping = ledger.inventory_get_mapping(doc.story_id)
    ref = mapping[1] if mapping and mapping[0] == host else None
    if ref is None:
        return SyncOutcome(doc.story_id, host, None, UNMAPPED)

    live = adapter.issue_view(ref)
    existing_body = live.body or ""

    # --- push: managed block (MD wins) + missing labels ---
    new_body = replace_managed_block(existing_body, doc)
    body_changed = new_body != existing_body

    desired = story_labels(doc.epic, doc.feature, doc.points, doc.risk)
    pushed_status = status_label(ledger.inventory_get_status(doc.story_id))
    if pushed_status:
        desired.append(pushed_status)
    missing = [label for label in desired if label not in live.labels]

    action = NOOP
    if body_changed or missing:
        adapter.issue_update(
            ref,
            body=new_body if body_changed else None,
            labels=missing,
        )
        action = PUSHED

    # --- pull: assignee → owner, human label → human_status ---
    owner = live.assignees[0] if live.assignees else None
    human_status = human_status_from_labels(live.labels)
    ledger.inventory_set_owner(doc.story_id, owner)
    ledger.inventory_set_human_status(doc.story_id, human_status)

    return SyncOutcome(
        doc.story_id, host, ref, action, owner=owner, human_status=human_status
    )


def sync_stories(
    adapter: IssueHostAdapter, ledger: Ledger, docs: list[StoryDoc]
) -> list[SyncOutcome]:
    """Reconcile a batch of stories in order, each via :func:`sync_story`.

    All host calls route through the single adapter so the pass is rate-limit
    aware at one seam and resumes cheaply after an interruption (already-current
    issues are no-ops). Returns one :class:`SyncOutcome` per input doc. This is
    the same reconcile engine ``sdlc issues init`` (Story 22.3-001) builds on.
    """
    return [sync_story(adapter, ledger, doc) for doc in docs]
