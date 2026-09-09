# ABOUTME: Best-effort build-loop ↔ host-issue integration — Closes #N close-link + live status.
# ABOUTME: Story 22.4-002 — a story's issue moves on its own as the build runs; never blocks a build.

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sdlc.issue_host import (
    FORGE_OVERRIDE_FILENAME,
    GITHUB_CR_TERMS,
    ChangeRequestChecks,
    ChangeRequestTerms,
    IssueHostError,
    Runner,
    declared_instance_for,
    get_adapter,
    load_repo_forge_declaration,
    resolve_forge,
)
from sdlc.story_render import story_marker

if TYPE_CHECKING:  # avoid a runtime import cycle with build.py (which imports this)
    from sdlc.build import Ledger

log = logging.getLogger(__name__)

__all__ = [
    "stage_status",
    "close_link",
    "change_request_terms",
    "change_request_status",
    "change_request_checks",
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


# Issue #677: the `story_inventory` mapping is a **per-checkout** cache, written
# only by `sdlc issues init`. A story mirrored from another checkout (or any
# fresh clone) reads no row here, which silently cost the PR its `Closes #N` and
# skipped the merge CI gate. The marker search that recovers it is a live host
# call, so its outcome — the found ref, or None for a genuine miss — is cached
# for the process: the inventory write-back below only persists when this
# checkout actually has a row for the story, and a story authored elsewhere has
# none. Keyed by ledger path so parallel cohorts on distinct ledgers never share.
_RECOVERED_REFS: dict[tuple[str, str], str | None] = {}


def _mirrors_stories(ledger: "Ledger") -> bool:
    """True when this checkout's inventory maps at least one story to an issue.

    The evidence gate for the recovery path: a repo that never ran
    ``sdlc issues init`` has no mirror to recover from, so it must keep today's
    zero-host-call behaviour rather than pay a live marker search (and emit a
    warn) on every seam of every build.
    """
    return ledger.inventory_any_mapped()


def _repo_adapter(ledger: "Ledger", runner: Runner | None):
    """The adapter for the repo's *own* forge, or None when this checkout doesn't mirror.

    Resolved exactly as :func:`sdlc.build._open_story_cr` resolves the forge it
    opens the change request on — the repo's declaration/remote, not the story's
    inventory mapping — so it is available even when the mapping is missing.
    Raises :class:`IssueHostError` when the forge itself cannot be resolved.
    """
    if not _mirrors_stories(ledger):
        return None
    resolution = resolve_forge(Path.cwd())
    return get_adapter(
        resolution.host, runner=runner, instance_url=resolution.instance_url
    )


def _recover_ref(
    ledger: "Ledger", story_id: str, adapter, run_id: str | None
) -> str | None:
    """Re-discover a story's issue on the host by its body marker, or None.

    The same mechanism :func:`sdlc.story_mirror.mirror_story` already uses to
    recover a stale ref, applied to the case it never covered: a ref that is
    simply *absent* from this checkout. A hit is written back to the inventory so
    later runs in this checkout resolve locally; a genuine miss is recorded as a
    ``warn`` event naming the story rather than a silent debug log (issue #677).
    """
    key = (str(ledger.db_path), story_id)
    if key in _RECOVERED_REFS:
        return _RECOVERED_REFS[key]
    found = adapter.issue_find(story_marker(story_id))
    ref = found.ref if found is not None else None
    _RECOVERED_REFS[key] = ref
    if ref is not None:
        # A no-op when this checkout has no inventory row for the story — the
        # process cache above is then the only thing keeping the search to one.
        ledger.inventory_set_mapping(story_id, adapter.host, ref)
    else:
        _warn_unmapped(ledger, run_id, story_id)
    return ref


def _warn_unmapped(ledger: "Ledger", run_id: str | None, story_id: str) -> None:
    """Surface a story with no host issue as a ``warn`` event naming it (issue #677)."""
    message = (
        f"story mirror: no host issue found for story {story_id} (not in this "
        "checkout's inventory and no marker match on the host) — close-link and "
        "issue lifecycle skipped; run `sdlc issues init` to mirror it"
    )
    if not run_id:
        log.debug(message)
        return
    try:
        ledger.event_log(run_id, story_id, "warn", "controller", message)
    except Exception:  # noqa: BLE001 — best-effort; a ledger hiccup never fails a build
        log.debug("unmapped-story warn failed for %s", story_id, exc_info=True)


def _adapter_and_ref(
    ledger: "Ledger", story_id: str, runner: Runner | None, run_id: str | None = None
):
    """Return ``(adapter, ref)`` for a story's mapped issue, or None when unmapped.

    Resolves the host from the inventory mapping and builds its adapter. When the
    story has no mapping *and* this checkout mirrors at all, the issue is
    re-discovered on the host by its body marker and the mapping cached
    (issue #677). Returns None — never raises — when nothing resolves or the
    recorded host is unsupported, so every caller degrades to a clean no-op.

    Story 30.1-001: the inventory records the forge *kind* only, so the repo's
    `.sdlc-forge.yaml` supplies the self-hosted instance for that kind (the same
    host-vs-instance split :func:`~sdlc.issue_host.resolve_forge` applies to an
    explicit override). Without it every mirror-lifecycle call here — close-link,
    CR terms, the merge gate's CI poll, status announcements — would build a
    GitLab adapter with no ``GITLAB_HOST`` and target gitlab.com on precisely the
    local-forge repos the declaration exists for. The declaration is read from
    the process cwd because that is where these adapters' `gh`/`glab`
    subprocesses run — every caller here uses the default (cwd-inheriting)
    runner, so cwd's repo is the one being talked to.
    """
    mapping = ledger.inventory_get_mapping(story_id)
    try:
        if mapping is None:
            adapter = _repo_adapter(ledger, runner)
            if adapter is None:
                return None
            ref = _recover_ref(ledger, story_id, adapter, run_id)
            return (adapter, ref) if ref is not None else None
        host, ref = mapping
        declaration = load_repo_forge_declaration(
            override_path=Path.cwd() / FORGE_OVERRIDE_FILENAME
        )
        instance_url = declared_instance_for(declaration, host)
        return get_adapter(host, runner=runner, instance_url=instance_url), ref
    except IssueHostError:
        # An unsupported recorded host, or a malformed declaration — a build
        # aborts on the latter at preflight (`build._forge_declaration_error`),
        # so reaching it here means a best-effort caller that must no-op rather
        # than guess an instance and talk to the wrong forge.
        return None


def _cr_adapter(ledger: "Ledger", story_id: str, runner: Runner | None):
    """The adapter to read a story's *change request* with, or None.

    A CR lookup needs a host adapter and the CR number the caller already holds —
    the story's issue mapping only refines *which* host. Issue #677: routing it
    through the mapping meant a missing mirror row made the merge CI gate see an
    unresolvable status and skip, even though the PR (and its forge) were known.
    So an unmapped story falls back to the repo's own forge. A story mapped to a
    host that will not resolve is *not* re-guessed onto another forge — the
    recorded host is a deliberate choice, and talking to the wrong one is worse
    than degrading.
    """
    got = _adapter_and_ref(ledger, story_id, runner)
    if got is not None:
        return got[0]
    if ledger.inventory_get_mapping(story_id) is not None:
        return None
    try:
        return _repo_adapter(ledger, runner)
    except IssueHostError:
        return None


def close_link(
    ledger: "Ledger",
    story_id: str,
    *,
    runner: Runner | None = None,
    run_id: str | None = None,
) -> str | None:
    """``Closes #<ref>`` for a story's mapped issue, or None when unmapped (AC1).

    The build injects this into the PR description so merging the PR auto-closes
    the story's tracking issue. Best-effort: any lookup/host failure (including a
    ledger without the inventory table) yields None and the PR is opened without
    a close-link — a build is never blocked on the mirror.

    This is the once-per-story resolution point, so ``run_id`` is threaded here
    (and only here) to log the one ``warn`` event for a story that resolves to no
    host issue at all — the later seams reuse the same cached outcome (#677).
    """
    try:
        got = _adapter_and_ref(ledger, story_id, runner, run_id)
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
        adapter = _cr_adapter(ledger, story_id, runner)
        if adapter is None:
            return GITHUB_CR_TERMS
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
        adapter = _cr_adapter(ledger, story_id, runner)
        if adapter is None:
            return None
        return adapter.cr_status(str(cr_ref))
    except Exception:  # noqa: BLE001 — best-effort; a host hiccup never fails a build
        log.debug("change_request_status failed for %s", story_id, exc_info=True)
        return None


def change_request_checks(
    ledger: "Ledger", story_id: str, cr_ref: object, *, runner: Runner | None = None
) -> ChangeRequestChecks | None:
    """A story CR's labels + named per-check statuses, or None (Story 25.1-001).

    The deterministic feed for gate-only-block recognition: when a merge fails,
    the controller re-checks the CR itself — the ``risk:high``/``risk-approved``
    labels plus *which* named check is red — so a merge blocked solely by the
    high-risk approval gate parks ``AWAITING_APPROVAL`` even when the agent's
    free text or a pre-dispatch CI-gate block loses the signal. Best-effort: an
    unmapped story, an unsupported host, or any host failure yields None —
    never raises — so the caller conservatively treats the failure as a real
    merge failure (no false-positive parking).
    """
    try:
        adapter = _cr_adapter(ledger, story_id, runner)
        if adapter is None:
            return None
        return adapter.cr_checks(str(cr_ref))
    except Exception:  # noqa: BLE001 — best-effort; a host hiccup never fails a build
        log.debug("change_request_checks failed for %s", story_id, exc_info=True)
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
