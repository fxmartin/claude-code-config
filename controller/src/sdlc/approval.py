# ABOUTME: Read-only change-request approval probe for the approval-aware queue.
# ABOUTME: Story 32.2-002 — "is PR #N approved / merged / closed?", host-agnostic.

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sdlc.issue_host import (
    IssueHostError,
    Runner,
    get_adapter,
    repo_runner,
    resolve_forge,
)
from sdlc.risk_gate import RISK_APPROVED_LABEL

__all__ = ["ApprovalVerdict", "poll_approval"]

log = logging.getLogger(__name__)

# The two signals that release a high-risk park, in the order the story names
# them: FX's `risk-approved` label, or an approving review on the change
# request. Both mean the same thing — a human said yes — so either resumes the
# run; the winning one is reported so the notification says which.
_LABEL_SIGNAL = f"{RISK_APPROVED_LABEL} label"
_REVIEW_SIGNAL = "approving review"


@dataclass(frozen=True)
class ApprovalVerdict:
    """What one read of a parked job's change request said.

    ``state`` is the normalised CR state (``open``/``closed``/``merged``);
    ``approved`` is true when a human has released the high-risk gate; ``signal``
    names which of the two signals did it, and is empty when neither did.

    Deliberately a *verdict about the CR*, not an instruction: what the queue
    does with a merged-but-unapproved CR (reconcile) versus an approved open one
    (resume) is the scheduler's decision, so this stays a pure read.
    """

    state: str
    approved: bool
    signal: str


def poll_approval(
    root: "str | Path", pr_number: int, *, runner: Runner | None = None
) -> ApprovalVerdict | None:
    """Read change request ``pr_number`` in ``root``; None when it cannot be read.

    Read-only by construction: it resolves ``root``'s host from its git remote,
    builds that host's adapter, and calls the single ``cr_approval`` verb —
    `gh pr view` / `glab mr view`. Nothing here merges, labels, comments on or
    closes anything; the merge that follows an approval is performed by the
    resumed run's own merge stage, through the controller, exactly as it would
    have been last night.

    Best-effort, and the "None" case matters: an absent CLI, an unauthenticated
    host, a network blip, a rate-limit rejection, an undetectable forge or a
    malformed `.sdlc-forge.yaml` all
    return None rather than raising, and the caller leaves the job parked. The
    alternative — reading a failed lookup as "closed" — would fail a job over a
    dropped packet.

    ``runner`` overrides the host-CLI seam; by default the CLI is invoked
    *inside* ``root``, since both CLIs infer the repository from the working
    directory and the scheduler polls several repos from one process.
    """
    try:
        # Story 30.1-001: resolve the forge *and* any declared self-hosted
        # instance, so a parked job in a local-forge repo polls that instance
        # instead of gitlab.com (which answers None and parks it forever).
        resolution = resolve_forge(root)
        adapter = get_adapter(
            resolution.host,
            runner=runner or repo_runner(root),
            instance_url=resolution.instance_url,
        )
        view = adapter.cr_approval(str(pr_number))
    except IssueHostError:
        log.debug("approval poll failed for #%s in %s", pr_number, root, exc_info=True)
        return None
    except Exception:  # noqa: BLE001 — a poll must never take the scheduler down
        log.debug("approval poll crashed for #%s in %s", pr_number, root, exc_info=True)
        return None

    if not view.state:
        # No state at all means the read did not actually land (empty/garbage
        # payload). Treating that as "open, unapproved" would be a lie with a
        # 5-minute cost; treating it as unreadable is honest.
        return None

    labels = {label.strip().lower() for label in view.labels}
    if RISK_APPROVED_LABEL in labels:
        return ApprovalVerdict(state=view.state, approved=True, signal=_LABEL_SIGNAL)
    if view.approved:
        return ApprovalVerdict(state=view.state, approved=True, signal=_REVIEW_SIGNAL)
    return ApprovalVerdict(state=view.state, approved=False, signal="")
