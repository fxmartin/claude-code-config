# ABOUTME: Resolve the developer's identity from host auth; cache run actor + story owner.
# ABOUTME: Story 22.5-001 — the code host IS the identity provider; no shared token, degrades to `unknown`.

from __future__ import annotations

from sdlc.build import Ledger
from sdlc.issue_host import Issue, IssueHostAdapter, IssueHostError

__all__ = [
    "UNKNOWN_ACTOR",
    "resolve_actor",
    "owner_from_issue",
    "cache_actor",
    "cache_owner",
]

# The actor stamped when host identity cannot be resolved — no CLI, not
# authenticated, or a blank login. Story 22.5-001 AC3: identity degrades
# gracefully rather than crashing a run, so attribution is always *some* value.
UNKNOWN_ACTOR = "unknown"


def resolve_actor(adapter: IssueHostAdapter) -> str:
    """Resolve the authenticated developer's login via the adapter's ``whoami``.

    The code host is the identity provider (`gh api user` / `glab` equivalent) —
    each developer authenticates as themselves, so there is no shared service
    account to attribute (the shared-token anti-pattern; see the docs). Returns
    the host login, or :data:`UNKNOWN_ACTOR` when host auth is absent or the CLI
    is missing — identity degrades, it never crashes the caller (AC3).
    """
    try:
        login = adapter.whoami()
    except IssueHostError:
        return UNKNOWN_ACTOR
    return login.strip() or UNKNOWN_ACTOR


def owner_from_issue(issue: Issue | None) -> str | None:
    """The owner cached from a host issue — its first assignee, or None.

    Targets GitHub/GitLab Free-tier single-assignee issues; the first assignee
    is the owner. ``None`` (no issue, or unassigned) clears the cache.
    """
    if issue is None or not issue.assignees:
        return None
    return issue.assignees[0]


def cache_actor(ledger: Ledger, run_id: str, adapter: IssueHostAdapter) -> str:
    """Resolve the host identity and stamp it as this run's actor (AC1).

    Resolved once per run from host auth and persisted on the run row so the
    ledger records *who* drove each story-run without a password or shared
    account. Returns the stamped actor (``unknown`` when host auth is absent).
    """
    actor = resolve_actor(adapter)
    ledger.run_set_actor(run_id, actor)
    return actor


def cache_owner(ledger: Ledger, story_id: str, issue: Issue | None) -> str | None:
    """Cache a story's owner from its host issue's assignee (AC2).

    A cached *read* of the host assignee — the host stays authoritative — so a
    local build can show or skip by owner without an API call. Returns the
    cached owner (``None`` when the issue is unassigned, which clears it).
    """
    owner = owner_from_issue(issue)
    ledger.inventory_set_owner(story_id, owner)
    return owner
