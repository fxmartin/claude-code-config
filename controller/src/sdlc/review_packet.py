# ABOUTME: Pre-baked review packet (Story 27.3-003) — CR meta, changed files, diff, pipeline signals.
# ABOUTME: Deterministic builder behind the review prompts and the `sdlc review-packet` verb.

"""Build the deterministic packet the review stage embeds into its prompt.

Reviewers used to re-derive their inputs with ``gh pr view`` / ``gh pr diff`` /
``gh pr checkout`` round-trips on every dispatch. The packet bakes those inputs
once — change-request metadata, the changed-file list, the full unified diff,
and the pipeline's test/coverage signals — via the Epic-22/23 code-host adapter
so GitHub and GitLab render identically.

Size discipline: the full packet's diff body is **never truncated**. Past
:data:`PACKET_MAX_CHARS` the best-effort :func:`packet_result` degrades to a
diff-omitted summary tier (meta, checks, changed-file list, per-file
diffstat) instead of dropping the packet outright; only when even that
summary overflows the cap — or the host call itself fails — does it return no
packet, and the caller falls back to today's fetch-it-yourself instructions.
:func:`packet_block` is a back-compat wrapper returning just the rendered
text of whichever tier was used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from sdlc.issue_host import ChangeRequest, IssueHostAdapter, IssueHostError

log = logging.getLogger(__name__)

# Cap on the *rendered* packet. Typical story diffs render well under this;
# past it the embed degrades first to a diff-omitted summary tier, then (if
# even that overflows) to the fetch-it-yourself fallback. ~120k chars ≈ 30k
# tokens — large, but still far cheaper than a reviewer re-fetching and
# re-reading the same inputs across retries.
PACKET_MAX_CHARS = 120_000

PacketTier = Literal["full", "summary", "none"]


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

    def render_summary(self) -> str:
        """Render the diff-omitted summary tier: meta, checks, per-file diffstat.

        The fallback between the full packet and no packet at all — the diff
        body is what drives an oversized render, so this tier drops it and
        keeps everything the controller already has for free (meta, checks,
        changed-file list, per-file +added/-removed counts parsed from the
        same diff).
        """
        abbr = "MR" if self.meta.host == "gitlab" else "PR"
        source = self.meta.source_branch or "?"
        target = self.meta.target_branch or "?"
        checks = (
            self.checks
            if self.checks
            else "not available — run the project's test suite yourself if needed."
        )
        stats = {path: (added, removed) for path, added, removed in _diffstat(self.diff)}
        file_lines = "\n".join(
            f"- {path} (+{stats.get(path, (0, 0))[0]}/-{stats.get(path, (0, 0))[1]})"
            for path in self.files
        )
        return (
            "## Review Packet (summary — diff omitted, over the size cap)\n\n"
            f"- {abbr}: #{self.meta.ref} — {self.meta.title}\n"
            f"- URL: {self.meta.url}\n"
            f"- State: {self.meta.state}\n"
            f"- Branch: {source} → {target}\n\n"
            "### Test & coverage signals\n"
            f"{checks}\n\n"
            f"### Changed files ({len(self.files)}), with +added/-removed line counts\n"
            f"{file_lines}\n\n"
            "### Diff\n"
            "Omitted — the full packet exceeded the size cap. Fetch per-file "
            f"diffs as needed (e.g. `{'glab mr diff' if self.meta.host == 'gitlab' else 'gh pr diff'} "
            f"{self.meta.ref} -- <path>` or `git diff origin/{target}...HEAD -- <path>`).\n"
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


def _diffstat(diff: str) -> tuple[tuple[str, int, int], ...]:
    """Per-file ``(path, added, removed)`` line counts from a unified diff.

    Parses the diff already fetched for the full packet — no extra host call
    — so the summary tier can show change shape without the diff body.
    """
    counts: dict[str, list[int]] = {}
    order: list[str] = []
    current: str | None = None
    marker = " b/"
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            at = line.rfind(marker)
            current = line[at + len(marker):] if at != -1 else None
            if current is not None and current not in counts:
                counts[current] = [0, 0]
                order.append(current)
            continue
        if current is None or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            counts[current][0] += 1
        elif line.startswith("-"):
            counts[current][1] += 1
    return tuple((path, counts[path][0], counts[path][1]) for path in order)


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


@dataclass(frozen=True)
class PacketResult:
    """The tiered outcome of a best-effort packet build.

    ``tier`` names which render ``text`` came from: ``"full"`` (diff
    included), ``"summary"`` (diff omitted, past the cap), or ``"none"``
    (caller must fall back — ``text`` is ``None``). ``full_chars`` is the
    rendered size of the *full* packet whenever one could be built — evidence
    for tuning :data:`PACKET_MAX_CHARS` — and is ``None`` only when the host
    call itself failed or the diff was empty, so no render was possible.
    """

    text: str | None
    tier: PacketTier
    full_chars: int | None


def packet_result(
    adapter: IssueHostAdapter,
    cr_ref: str,
    *,
    checks: str | None = None,
    max_chars: int = PACKET_MAX_CHARS,
) -> PacketResult:
    """Bake the best-effort packet, degrading full -> summary -> none.

    The full packet (diff included) is tried first; past ``max_chars`` it
    degrades to the diff-omitted summary tier (meta, checks, changed files,
    per-file diffstat); if even that overflows the cap, the caller must fall
    back to fetch-it-yourself review. A host failure or empty diff skips
    straight to ``"none"`` with no size recorded.
    """
    try:
        packet = build_review_packet(adapter, cr_ref, checks=checks)
    except Exception:  # noqa: BLE001 — best-effort; the prompt has a fallback path
        log.debug("review packet build failed for %s", cr_ref, exc_info=True)
        return PacketResult(text=None, tier="none", full_chars=None)
    full = packet.render()
    if len(full) <= max_chars:
        return PacketResult(text=full, tier="full", full_chars=len(full))
    summary = packet.render_summary()
    if len(summary) <= max_chars:
        log.debug(
            "review packet for %s degraded to summary tier: full %d chars, "
            "summary %d chars (cap %d)",
            cr_ref, len(full), len(summary), max_chars,
        )
        return PacketResult(text=summary, tier="summary", full_chars=len(full))
    log.debug(
        "review packet for %s unavailable: full %d chars, summary %d chars "
        "(cap %d) — falling back",
        cr_ref, len(full), len(summary), max_chars,
    )
    return PacketResult(text=None, tier="none", full_chars=len(full))


def packet_block(
    adapter: IssueHostAdapter,
    cr_ref: str,
    *,
    checks: str | None = None,
    max_chars: int = PACKET_MAX_CHARS,
) -> str | None:
    """Back-compat wrapper: the rendered packet (full or summary tier), or
    ``None`` when the caller must fall back. See :func:`packet_result` for
    the tier + size evidence this discards.
    """
    return packet_result(adapter, cr_ref, checks=checks, max_chars=max_chars).text
