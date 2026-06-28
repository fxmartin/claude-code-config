# ABOUTME: Assign a single story or a whole epic to a host user — the human-write-back lane.
# ABOUTME: Story 22.5-002 — thin command over the adapter's issue_assign; epic-id cascades to every story.

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sdlc.build import Ledger
from sdlc.issue_host import IssueHostAdapter

__all__ = [
    "AssignError",
    "AssignResult",
    "is_epic_target",
    "assign",
]

# `epic-22` / `EPIC-7` — the epic-cascade target. The number is the inventory
# `epic` column literal (Story 22.1-001).
_EPIC_TARGET = re.compile(r"^epic-(\d+)$", re.IGNORECASE)
# `22.5-002` — a single story id (epic.feature-seq), the established story form.
_STORY_TARGET = re.compile(r"^\d+\.\d+-\d+$")


class AssignError(Exception):
    """A fail-fast assignment error: bad target, empty/unknown user, empty epic."""


@dataclass(frozen=True)
class AssignResult:
    """The outcome of one assign command.

    ``assigned``/``already``/``unmapped`` are story-id lists (in inventory order).
    A non-empty ``unmapped`` means the command did real work but could not cover
    every requested story — the caller surfaces it and exits non-zero so a partial
    pass is never mistaken for a clean success (Story 22.5-002 AC3).
    """

    user: str
    target: str
    is_epic: bool
    assigned: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)


def is_epic_target(target: str) -> bool:
    """True when ``target`` is an ``epic-NN`` cascade target (vs a story id)."""
    return bool(_EPIC_TARGET.match(target or ""))


def _resolve_story_ids(ledger: Ledger, target: str) -> tuple[list[str], bool]:
    """Resolve a target to the story ids it covers and whether it is an epic.

    An ``epic-NN`` target enumerates every story in that epic from the inventory
    (Story 22.1-002 owns row creation); a story id resolves to itself. Raises
    :class:`AssignError` on a malformed target or an epic with no stories — the
    caller never half-runs against an empty/typo'd scope.
    """
    epic_match = _EPIC_TARGET.match(target or "")
    if epic_match:
        epic = epic_match.group(1)
        story_ids = ledger.inventory_stories_for_epic(epic)
        if not story_ids:
            raise AssignError(
                f"no stories found for {target!r} in the inventory — run the mirror "
                "first (`sdlc issues init`)"
            )
        return story_ids, True
    if _STORY_TARGET.match(target or ""):
        return [target], False
    raise AssignError(
        f"not a story id or epic id: {target!r} (expected NN.F-NNN or epic-NN)"
    )


def assign(
    adapter: IssueHostAdapter, ledger: Ledger, target: str, user: str
) -> AssignResult:
    """Assign a single story *or* a whole epic to ``user`` on the host.

    This is the one place a CLI writes ownership *to* the host (GitHub/GitLab stays
    authoritative; the inventory ``owner`` is the cached read). The flow, per Story
    22.5-002:

    1. **Fail fast** on an empty user, an unknown host user (the adapter's
       ``user_exists``), a malformed target, or an epic with no stories — before
       any assignment, so a typo never half-assigns a cascade.
    2. **Resolve** the target to its story ids (an ``epic-NN`` cascades to every
       story in the epic; a story id is just itself).
    3. **Assign** each mapped story through ``issue_assign`` and cache the
       ``owner``. A story already owned by ``user`` is a no-op (no host write).
       A story with no issue on this host is collected as ``unmapped`` and
       reported — never silently succeeded.

    Returns an :class:`AssignResult` describing the pass.
    """
    user = (user or "").strip()
    if not user:
        raise AssignError("a user must be given")
    # Validate the user once, up front, so an unknown user fails the whole command
    # rather than partially assigning a cascade (Story 22.5-002 AC3).
    if not adapter.user_exists(user):
        raise AssignError(f"unknown {adapter.host} user: {user!r}")

    story_ids, is_epic = _resolve_story_ids(ledger, target)
    host = adapter.host

    assigned: list[str] = []
    already: list[str] = []
    unmapped: list[str] = []

    for story_id in story_ids:
        mapping = ledger.inventory_get_mapping(story_id)
        # Only an issue mapped *on this host* is assignable here; a cross-host or
        # absent mapping is reported as unmapped.
        ref = mapping[1] if mapping and mapping[0] == host else None
        if ref is None:
            unmapped.append(story_id)
            continue
        # Idempotent: a story already owned by this user needs no host write
        # (re-assigning the same user is a no-op, AC4). The cache is the cheap
        # check — the host call itself is idempotent too.
        if ledger.inventory_get_owner(story_id) == user:
            already.append(story_id)
            continue
        adapter.issue_assign(ref, user)
        ledger.inventory_set_owner(story_id, user)
        assigned.append(story_id)

    return AssignResult(
        user=user,
        target=target,
        is_epic=is_epic,
        assigned=assigned,
        already=already,
        unmapped=unmapped,
    )
