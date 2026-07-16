# ABOUTME: Pre-baked review packet (Story 27.3-003) — CR meta, changed files, diff, pipeline signals.
# ABOUTME: Deterministic builder behind the review prompts and the `sdlc review-packet` verb.

"""Build the deterministic packet the review stage embeds into its prompt.

Reviewers used to re-derive their inputs with ``gh pr view`` / ``gh pr diff`` /
``gh pr checkout`` round-trips on every dispatch. The packet bakes those inputs
once — change-request metadata, the changed-file list, the full unified diff,
and the pipeline's test/coverage signals — via the Epic-22/23 code-host adapter
so GitHub and GitLab render identically.

Size discipline: an oversized packet is **never truncated**. The best-effort
:func:`packet_block` returns ``None`` past :data:`PACKET_MAX_CHARS` (or on any
host failure) and the caller falls back to today's fetch-it-yourself
instructions, so a reviewer never works from a silently incomplete diff.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sdlc.issue_host import ChangeRequest, IssueHostAdapter, IssueHostError

log = logging.getLogger(__name__)

# Cap on the *rendered* packet. Typical story diffs render well under this;
# past it the embed degrades to the fetch-it-yourself fallback rather than a
# truncated diff. ~120k chars ≈ 30k tokens — large, but still far cheaper than
# a reviewer re-fetching and re-reading the same inputs across retries.
PACKET_MAX_CHARS = 120_000


@dataclass(frozen=True)
class ReviewPacket:
    """The pre-baked review inputs for one change request."""

    meta: ChangeRequest
    files: tuple[str, ...]
    diff: str
    checks: str | None = None  # test/coverage signals, e.g. from the coverage stage

    def render(self) -> str:
        """Render the packet as the markdown block the review prompt embeds."""
        abbr = "MR" if self.meta.host == "gitlab" else "PR"
        source = self.meta.source_branch or "?"
        target = self.meta.target_branch or "?"
        checks = (
            self.checks
            if self.checks
            else "not available — run the project's test suite yourself if needed."
        )
        fence = _fence(self.diff)
        file_lines = "\n".join(f"- {path}" for path in self.files)
        return (
            "## Review Packet\n\n"
            f"- {abbr}: #{self.meta.ref} — {self.meta.title}\n"
            f"- URL: {self.meta.url}\n"
            f"- State: {self.meta.state}\n"
            f"- Branch: {source} → {target}\n\n"
            "### Test & coverage signals\n"
            f"{checks}\n\n"
            f"### Changed files ({len(self.files)})\n"
            f"{file_lines}\n\n"
            "### Diff\n"
            f"{fence}diff\n"
            f"{self.diff.rstrip()}\n"
            f"{fence}\n"
        )


def _fence(text: str) -> str:
    """A backtick fence strictly longer than any backtick run inside ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def changed_files(diff: str) -> tuple[str, ...]:
    """The changed paths named by a unified diff's ``diff --git`` headers.

    Takes the ``b/`` (post-image) side — on a rename that is the destination
    the reviewer reads — deduped in first-seen order. Parsing the diff keeps
    the file list host-neutral (no extra ``gh``/``glab`` call).
    """
    files: list[str] = []
    marker = " b/"
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        at = line.rfind(marker)
        if at != -1:
            files.append(line[at + len(marker):])
    return tuple(dict.fromkeys(files))


def build_review_packet(
    adapter: IssueHostAdapter,
    cr_ref: str,
    *,
    checks: str | None = None,
) -> ReviewPacket:
    """Bake the packet for ``cr_ref`` via the code-host adapter.

    Raises :class:`IssueHostError` on any host failure or an empty diff (an
    unreviewable change request must never yield a hollow packet).
    """
    meta = adapter.cr_view(cr_ref)
    diff = adapter.cr_diff(cr_ref)
    if not diff.strip():
        raise IssueHostError(f"change request {cr_ref} has an empty diff")
    return ReviewPacket(meta=meta, files=changed_files(diff), diff=diff, checks=checks)


def packet_block(
    adapter: IssueHostAdapter,
    cr_ref: str,
    *,
    checks: str | None = None,
    max_chars: int = PACKET_MAX_CHARS,
) -> str | None:
    """The rendered packet, or ``None`` when the caller must fall back.

    Best-effort: any host failure, an empty diff, or a rendered packet over
    ``max_chars`` yields ``None`` — the review prompt then keeps today's
    fetch-it-yourself instructions. Oversize is a fallback, never a truncation.
    """
    try:
        rendered = build_review_packet(adapter, cr_ref, checks=checks).render()
    except Exception:  # noqa: BLE001 — best-effort; the prompt has a fallback path
        log.debug("review packet build failed for %s", cr_ref, exc_info=True)
        return None
    if len(rendered) > max_chars:
        log.debug(
            "review packet for %s is %d chars (cap %d) — falling back",
            cr_ref, len(rendered), max_chars,
        )
        return None
    return rendered
