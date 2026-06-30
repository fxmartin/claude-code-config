# ABOUTME: Best-effort build-loop ↔ host-issue integration — Closes #N close-link + live status.
# ABOUTME: Story 22.4-002 — a story's issue moves on its own as the build runs; never blocks a build.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sdlc.issue_host import (
    GITHUB_CR_TERMS,
    ChangeRequestTerms,
    IssueHostError,
    Runner,
    get_adapter,
)

if TYPE_CHECKING:  # avoid a runtime import cycle with build.py (which imports this)
    from sdlc.build import Ledger

log = logging.getLogger(__name__)

__all__ = [
    "stage_status",
    "close_link",
    "change_request_terms",
    "change_request_status",
    "announce_status",
    "announce_terminal",
]

# Map a pipeline stage to the coarse, host-agnostic status slug shown on its
# issue (a `status:<slug>` label + a short comment). ``build`` and ``coverage``
# share ``building`` so advancing build→coverage is a silent no-op — no duplicate
# comment, no redundant label churn — and the visible transitions are exactly
# building → in-review → merging as the pipeline runs.
_STAGE_STATUS = {
    "build": "building",
    "coverage": "building",
    "review": "in-review",
    "merge": "merging",
}

# Map a *terminal* story status to a status slug. DONE is intentionally absent:
# the merge's ``Closes #N`` auto-closes the issue, so a separate "done" comment
# would be redundant. RATE_LIMITED/BLOCKED are transient/scheduling states, not a
# human-actionable issue signal, so they are omitted too.
_TERMINAL_STATUS = {
    "NEEDS_ATTENTION": "needs-attention",
    "FAILED": "failed",
    "AWAITING_APPROVAL": "awaiting-approval",
}

# The full closed set of status slugs this module ever stamps. announce_status
# adds exactly one and removes every *other* one, so an issue carries a single
# live ``status:<slug>`` label without the caller threading prior state.
_ALL_STATUSES = (
    "building",
    "in-review",
    "merging",
    "needs-attention",
    "failed",
    "awaiting-approval",
)

_STATUS_LABEL = "status:{}"


def stage_status(stage: str) -> str | None:
    """The status slug for a pipeline ``stage`` (``building``/``in-review``/…), or None."""
    return _STAGE_STATUS.get(stage)


def _adapter_and_ref(ledger: "Ledger", story_id: str, runner: Runner | None):
    """Return ``(adapter, ref)`` for a story's mapped issue, or None when unmapped.

    Resolves the host from the inventory mapping and builds its adapter. Returns
    None — never raises — when the story has no mapping or the recorded host is
    unsupported, so every caller degrades to a clean no-op.
    """
    mapping = ledger.inventory_get_mapping(story_id)
    if mapping is None:
        return None
    host, ref = mapping
    try:
        return get_adapter(host, runner=runner), ref
    except IssueHostError:
        return None


def close_link(
    ledger: "Ledger", story_id: str, *, runner: Runner | None = None
) -> str | None:
    """``Closes #<ref>`` for a story's mapped issue, or None when unmapped (AC1).

    The build injects this into the PR description so merging the PR auto-closes
    the story's tracking issue. Best-effort: any lookup/host failure (including a
    ledger without the inventory table) yields None and the PR is opened without
    a close-link — a build is never blocked on the mirror.
    """
    try:
        got = _adapter_and_ref(ledger, story_id, runner)
        if got is None:
            return None
        adapter, ref = got
        return adapter.close_keyword(ref)
    except Exception:  # noqa: BLE001 — best-effort; a host hiccup never fails a build
        log.debug("close_link failed for %s", story_id, exc_info=True)
        return None


def change_request_terms(
    ledger: "Ledger", story_id: str, *, runner: Runner | None = None
) -> ChangeRequestTerms:
    """The host-correct change-request terms for a story's target (Story 23.2-001).

    Resolves the story's mapped host and returns its adapter's
    :class:`~sdlc.issue_host.ChangeRequestTerms` — ``MR``/`glab` for a GitLab
    target, ``PR``/`gh` for GitHub. Best-effort: an unmapped story, an unsupported
    host, or any lookup failure (including a ledger without the inventory table)
    falls back to :data:`~sdlc.issue_host.GITHUB_CR_TERMS`, so the GitHub path is
    byte-identical to today (AC2) and a host hiccup never blocks a build.
    """
    try:
        got = _adapter_and_ref(ledger, story_id, runner)
        if got is None:
            return GITHUB_CR_TERMS
        adapter, _ = got
        return adapter.cr_terms
    except Exception:  # noqa: BLE001 — best-effort; a host hiccup never fails a build
        log.debug("change_request_terms failed for %s", story_id, exc_info=True)
        return GITHUB_CR_TERMS


def change_request_status(
    ledger: "Ledger", story_id: str, cr_ref: object, *, runner: Runner | None = None
) -> str | None:
    """The normalised CI status of a story's open change request, or None (Story 23.2-002).

    Resolves the story's mapped host, builds its adapter, and reads the
    change request ``cr_ref``'s CI/pipeline status (`gh pr` checks rollup / the
    GitLab MR pipeline) normalised to one of :data:`~sdlc.issue_host.CR_SUCCESS`
    etc. The merge gate (:func:`sdlc.build._run_merge_ci_gate`) polls this to
    decide whether the merge may proceed. Best-effort: an unmapped story, an
    unsupported host, or any host failure yields None — never raises — so the
    gate degrades to a clean no-op (today's agent-driven merge) rather than
    blocking a build on a mirror hiccup.
    """
    try:
        got = _adapter_and_ref(ledger, story_id, runner)
        if got is None:
            return None
        adapter, _ = got
        return adapter.cr_status(str(cr_ref))
    except Exception:  # noqa: BLE001 — best-effort; a host hiccup never fails a build
        log.debug("change_request_status failed for %s", story_id, exc_info=True)
        return None


def announce_status(
    ledger: "Ledger",
    story_id: str,
    status: str | None,
    *,
    runner: Runner | None = None,
) -> str | None:
    """Move a story's issue to ``status`` — a short comment + a ``status:`` label (AC2).

    On a stage transition the controller posts a short comment (attributed to the
    running developer's own host identity — the comment is made via their
    ``gh``/``glab`` auth, no shared token) and stamps the live ``status:<slug>``
    label, removing every other status label so the issue shows one current state.
    The comment and the label are independent best-effort lanes: one failing never
    suppresses the other.

    Returns the applied slug on success, else None — a story with no mapped issue,
    a None/blank status, or any host failure is a logged no-op that never blocks
    the build (AC3).
    """
    if not status:
        return None
    try:
        got = _adapter_and_ref(ledger, story_id, runner)
    except Exception:  # noqa: BLE001
        log.debug("announce_status lookup failed for %s", story_id, exc_info=True)
        return None
    if got is None:
        return None
    adapter, ref = got

    applied: str | None = None
    # Lane 1: the human-readable live comment (the primary signal).
    try:
        adapter.issue_comment(ref, _status_comment(story_id, status))
        applied = status
    except Exception:  # noqa: BLE001
        log.debug("status comment failed for %s", story_id, exc_info=True)
    # Lane 2: the single live status label (a board/list filter hint). Status
    # labels may not be provisioned in the repo, so a failure here is expected and
    # tolerated — the comment above is the authoritative live signal.
    try:
        label = _STATUS_LABEL.format(status)
        remove = [_STATUS_LABEL.format(s) for s in _ALL_STATUSES if s != status]
        adapter.issue_update(ref, labels=[label], remove_labels=remove)
        applied = status
    except Exception:  # noqa: BLE001
        log.debug("status label update failed for %s", story_id, exc_info=True)
    return applied


def announce_terminal(
    ledger: "Ledger",
    story_id: str,
    outcome: str,
    *,
    runner: Runner | None = None,
) -> str | None:
    """Announce a story's terminal ``outcome`` (NEEDS_ATTENTION/FAILED/…) on its issue.

    A thin wrapper over :func:`announce_status` that maps the terminal story
    status to its slug. DONE/RATE_LIMITED/BLOCKED map to nothing and no-op (DONE
    auto-closes via the PR's ``Closes #N``). Best-effort throughout.
    """
    return announce_status(
        ledger, story_id, _TERMINAL_STATUS.get(outcome), runner=runner
    )


def _status_comment(story_id: str, status: str) -> str:
    """The short, host-neutral status comment posted on a transition."""
    return f"Status: **{status}** — automated build update for story {story_id}."
