# ABOUTME: Close-out reconciliation — verify parked stories actually landed on
# ABOUTME: origin/main before the run terminal is computed (Story 12.3-001).

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sdlc.build import _STAGES, Ledger, _base_ref, _git

if TYPE_CHECKING:
    from sdlc.registry import Registry

__all__ = ["ReconcileResult", "reconcile_run"]

# Statuses that an unattended run can park a story in even though its PR may have
# genuinely merged. Reconciliation re-checks each of these against origin/main.
_PARKED = {"NEEDS_ATTENTION", "FAILED", "BLOCKED", "AWAITING_APPROVAL"}


@dataclass
class ReconcileResult:
    """The outcome of a reconciliation pass over one run.

    ``reclassified`` holds one ``{story_id, from_status, signal, sha}`` dict per
    story flipped to ``DONE``. ``skipped`` is true when an offline ``git fetch``
    forced a no-op (the run is left exactly as it was). ``fetched`` records
    whether the remote refresh actually ran and succeeded.
    """

    run_id: str
    reclassified: list[dict] = field(default_factory=list)
    run_status_before: str | None = None
    run_status_after: str | None = None
    fetched: bool = False
    skipped: bool = False

    @property
    def changed(self) -> bool:
        """Whether reconciliation altered any per-story or run status."""
        return bool(self.reclassified) or (
            self.run_status_before != self.run_status_after
        )


def _gh_pr_state(pr_number: int, root: Path) -> str | None:
    """The GitHub state of ``pr_number`` (e.g. ``MERGED``), or None.

    Best-effort and isolated so tests can monkeypatch it: returns None — never
    raises — when ``gh`` is absent, unauthenticated, offline, or the PR is
    unknown, so the gh signal simply does not fire.
    """
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state", "-q", ".state"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _gh_pr_for_landing(story_id: str, sha: str, root: Path) -> int | None:
    """The merged PR behind a detected landing, or None.

    Resolves the PR best-effort for landings where no ``pr_number`` was on file
    (the is-ancestor / git-cherry / commit-tag detectors). It first searches the
    story's own ``feature/<story_id>`` head ref — which is anchored to *this*
    story and so cannot attach a sibling's PR — and only falls back to the
    landing ``sha`` if that yields nothing. The sha fallback matters because some
    signals (git-cherry / gh-pr-merged) hand us the *base tip* sha, whose
    associated PR may belong to a sibling story; trying the head ref first keeps
    that fallback from misattributing. Isolated so tests can monkeypatch it.

    Best-effort and offline-safe: returns None — never raises — when ``gh`` is
    absent, unauthenticated, offline, or no PR matches, so PR backfill simply
    does not happen and the caller leaves ``pr_number`` as-is.
    """
    branch = f"feature/{story_id}"
    searches = [f"head:{branch}"] + ([sha] if sha else [])
    for query in searches:
        try:
            out = subprocess.run(
                [
                    "gh", "pr", "list", "--state", "merged", "--search", query,
                    "--json", "number", "-q", ".[0].number", "--limit", "1",
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            continue
        text = out.stdout.strip()
        if text:
            try:
                return int(text)
            except ValueError:
                continue
    return None


def _rev(root: Path, ref: str) -> str:
    """The resolved SHA of ``ref``, or "" when it cannot be resolved."""
    out = _git(root, "rev-parse", "--verify", "--quiet", ref)
    return out.stdout.strip() if out.returncode == 0 else ""


def _branch_owns_story_commit(root: Path, branch: str, story_id: str) -> bool:
    """Whether ``branch`` carries ≥1 commit tagged ``(#<story_id>)`` of its own.

    An empty/stacked-base-only branch is trivially an ancestor of base but holds
    none of the story's own work, so its tip's ``(#<story_id>)`` tag is absent
    (the commit it points at belongs to a sibling). Requiring the branch to
    carry a commit attributable to *this* story blocks that is-ancestor
    false-positive (#111) across every merge style (ff / merge-commit / squash),
    where ``base..branch`` count alone would be 0 once the work has landed.

    Returns False — never raises — on a non-zero ``git log`` so a flaky result
    degrades to "no own work" rather than a false landing.
    """
    out = _git(
        root, "log", branch, "--fixed-strings", f"--grep=(#{story_id})",
        "--format=%H", "-1",
    )
    return out.returncode == 0 and bool(out.stdout.strip())


def _detect_landing(
    story_id: str, pr_number: int | None, base: str | None, root: Path
) -> tuple[str, str] | None:
    """Whether ``feature/<story_id>``'s work is present on ``base`` / merged.

    Returns ``(signal, sha)`` for the first matching detector, else None. The
    detectors are deliberately complementary across merge styles:

    - ``is-ancestor`` — fast-forward / merge-commit landings (branch tip is an
      ancestor of base *and* the branch owns a commit tagged for this story, so
      an empty stacked branch cannot false-positive — #111).
    - ``git-cherry`` — patch-id equivalence (rebase / single-commit squash /
      transitive-stacked landings, where the tip sha differs but the patch is on
      base already).
    - ``gh-pr-merged`` — the PR shows ``MERGED`` even when the branch was deleted
      after merge (no local ref to inspect).
    - ``commit-tag`` — base carries a commit whose message holds the mandated
      ``(#<story_id>)`` tag (multi-commit squash, where patch-id no longer
      matches).

    ``story_commit_exists`` only counts commits *ahead of* base and so cannot see
    already-landed work; this combination is exactly the gap-closer.
    """
    branch = f"feature/{story_id}"
    branch_exists = (
        _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode
        == 0
    )

    if branch_exists and base:
        # An empty/stacked-base-only branch is trivially an ancestor of base but
        # carries none of the story's own work — guard that false positive (#111)
        # by requiring the branch to own a commit tagged for *this* story before
        # trusting is-ancestor.
        if (
            _git(root, "merge-base", "--is-ancestor", branch, base).returncode == 0
            and _branch_owns_story_commit(root, branch, story_id)
        ):
            return "is-ancestor", _rev(root, branch)
        cherry = _git(root, "cherry", base, branch)
        if cherry.returncode == 0:
            lines = [ln for ln in cherry.stdout.splitlines() if ln.strip()]
            # '+' marks a commit not yet upstream; '-' a patch-id already on base.
            # All-applied (≥1 line, no '+') means the work landed under new shas.
            if lines and not any(ln.startswith("+") for ln in lines):
                return "git-cherry", _rev(root, base)

    if pr_number and _gh_pr_state(pr_number, root) == "MERGED":
        return "gh-pr-merged", _rev(root, base) if base else ""

    if base:
        tagged = _git(
            root, "log", base, "--fixed-strings", f"--grep=(#{story_id})",
            "--format=%H", "-1",
        )
        if tagged.returncode == 0 and tagged.stdout.strip():
            return "commit-tag", tagged.stdout.strip()

    return None


def _ensure_merge_done(
    ledger: Ledger, run_id: str, story_id: str, attempts: list[dict]
) -> None:
    """Guarantee a DONE ``merge`` stage row so resume/rollback see the landing.

    ``rollback._story_merged`` and ``compute_resume_plan`` key off a DONE
    ``merge`` attempt. If one already exists we leave it (no duplicate row);
    otherwise we promote the latest non-DONE merge attempt, or synthesize a fresh
    one.
    """
    merge_attempts = [a for a in attempts if a.get("name") == "merge"]
    if any(a.get("status") == "DONE" for a in merge_attempts):
        return
    if merge_attempts:
        attempt = max(int(a.get("attempt", 1)) for a in merge_attempts)
        ledger.stage_finish(
            run_id, story_id, "merge", attempt, "DONE", output_path="reconcile"
        )
    else:
        ledger.stage_start(run_id, story_id, "merge", 1)
        ledger.stage_finish(
            run_id, story_id, "merge", 1, "DONE", output_path="reconcile"
        )


def _ensure_stages_done(
    ledger: Ledger, run_id: str, story_id: str, attempts: list[dict]
) -> None:
    """Terminalize the non-merge pipeline stages of a reconciled-DONE story.

    A story that landed but parked may never have recorded a terminal attempt for
    its intermediate ``build`` / ``coverage`` / ``review`` stages, so the
    dashboard renders them ``PENDING`` forever on an otherwise-``DONE`` story
    (#105). For each such stage lacking a DONE attempt we promote the latest
    non-DONE attempt, or synthesize a fresh one marked ``output_path="reconcile"``
    — mirroring :func:`_ensure_merge_done`. ``merge`` is left to that helper.

    Idempotent: a stage that already has a DONE attempt is skipped (no duplicate
    row), so a re-run over an already-reconciled story is a no-op.
    """
    for stage in _STAGES:
        if stage == "merge":
            continue
        stage_attempts = [a for a in attempts if a.get("name") == stage]
        if any(a.get("status") == "DONE" for a in stage_attempts):
            continue
        if stage_attempts:
            attempt = max(int(a.get("attempt", 1)) for a in stage_attempts)
            ledger.stage_finish(
                run_id, story_id, stage, attempt, "DONE", output_path="reconcile"
            )
        else:
            ledger.stage_start(run_id, story_id, stage, 1)
            ledger.stage_finish(
                run_id, story_id, stage, 1, "DONE", output_path="reconcile"
            )


def _compute_terminal(statuses: dict[str, str]) -> str:
    """The run terminal implied by per-story statuses (mirrors run_build).

    A failed/blocked story makes the run FAILED; a run whose only not-yet-done
    leftovers are ``AWAITING_APPROVAL`` stays ``AWAITING_APPROVAL`` so a standalone
    ``sdlc reconcile`` over a not-yet-approved run never downgrades the honest
    awaiting-human signal (Story 12.3-003); any other not-yet-done leftover
    (NEEDS_ATTENTION / IN_PROGRESS, or a mix) makes it NEEDS_ATTENTION; only an
    all-DONE/SKIPPED run is DONE.
    """
    vals = list(statuses.values())
    if any(v in {"FAILED", "BLOCKED"} for v in vals):
        return "FAILED"
    leftover = [v for v in vals if v not in {"DONE", "SKIPPED"}]
    if leftover and all(v == "AWAITING_APPROVAL" for v in leftover):
        return "AWAITING_APPROVAL"
    if leftover:
        return "NEEDS_ATTENTION"
    return "DONE"


def reconcile_run(
    ledger: Ledger,
    run_id: str | None = None,
    root: Path | None = None,
    fetch: bool = True,
    registry: "Registry | None" = None,
) -> ReconcileResult:
    """Reconcile a run's parked stories against ``origin/main``, then re-terminal.

    For every story parked ``NEEDS_ATTENTION``/``FAILED``/``BLOCKED``/
    ``AWAITING_APPROVAL`` whose ``feature/<id>`` work is provably present on the
    base (see :func:`_detect_landing`), the story is reclassified ``DONE``, a DONE
    ``merge`` row is recorded/updated, and a ``source="reconcile"`` audit event
    names the winning signal + merge SHA. The run terminal is then recomputed
    from the reconciled per-story statuses.

    Contract: this never raises and never *fails* an otherwise-good run. A
    ``git fetch`` failure (offline / no remote) degrades to a no-op skip. Stories
    already ``DONE``/``SKIPPED`` are untouched, and a re-run over an
    already-reconciled run is idempotent (no flips, no duplicate rows).
    """
    root = root or Path.cwd()
    rid = run_id or ledger.latest_run_id()
    if rid is None:
        return ReconcileResult(run_id="")

    run_row = ledger.run_row(rid)
    before = run_row.get("status") if run_row else None

    rows = ledger.story_rows(rid)
    parked = [r for r in rows if r.get("status") in _PARKED]
    if not parked:
        # Nothing parkable to reconcile — skip the remote refresh entirely.
        ledger.event_log(rid, "", "info", "reconcile", "nothing to reconcile")
        return ReconcileResult(
            run_id=rid, run_status_before=before, run_status_after=before
        )

    fetched = False
    if fetch:
        try:
            fetched = _git(root, "fetch", "origin").returncode == 0
        except (OSError, subprocess.SubprocessError):
            fetched = False
        if not fetched:
            # Offline / no remote: we cannot trust local refs to reflect what
            # landed, so degrade to a no-op skip rather than risk a wrong flip.
            ledger.event_log(
                rid, "", "info", "reconcile",
                "git fetch failed (offline / no remote): skipped reconciliation",
            )
            return ReconcileResult(
                run_id=rid,
                run_status_before=before,
                run_status_after=before,
                skipped=True,
            )

    base = _base_ref(root)
    breakdown = ledger.stage_breakdown(rid)
    reclassified: list[dict] = []
    for r in parked:
        sid = r["story_id"]
        landing = _detect_landing(sid, r.get("pr_number"), base, root)
        if landing is None:
            continue
        signal, sha = landing
        ledger.set_story_status(rid, sid, "DONE")
        # Backfill the PR best-effort: keep an already-recorded number, else
        # resolve the merged PR behind the landing via gh. A failed/empty lookup
        # leaves pr_number as-is and never crashes (reconcile's contract).
        if r.get("pr_number") is None:
            pr = _gh_pr_for_landing(sid, sha, root)
            if pr is not None:
                ledger.set_story_pr(rid, sid, pr)
        story_attempts = breakdown.get(sid, [])
        _ensure_merge_done(ledger, rid, sid, story_attempts)
        _ensure_stages_done(ledger, rid, sid, story_attempts)
        ledger.event_log(
            rid, sid, "info", "reconcile",
            f"reconciled {r.get('status')} → DONE via {signal}: "
            f"feature/{sid} landed on {base or 'base'}"
            + (f" (merge {sha[:8]})" if sha else ""),
        )
        reclassified.append(
            {"story_id": sid, "from_status": r.get("status"), "signal": signal, "sha": sha}
        )

    # Story 28.1-003: a batch run's run-level phases (e.g. doc-update) hang their
    # stage on a story-less anchor row so their spend can be metered. That anchor
    # is not a story, and its status is outside the story vocabulary, so counting
    # it here would read as a permanent leftover and pin a fully-landed run to
    # NEEDS_ATTENTION. Real stories always carry an id; anchors never do.
    statuses = {
        row["story_id"]: row["status"]
        for row in ledger.story_rows(rid)
        if row["story_id"]
    }
    after = _compute_terminal(statuses)
    completed = sum(1 for v in statuses.values() if v == "DONE")
    failed = sum(1 for v in statuses.values() if v == "FAILED")
    ledger.run_update_counts(rid, completed, failed)
    ledger.run_update_status(rid, after)
    # Mirror the normal build close-out: stamp the host registry so the
    # dashboard reflects the reconciled terminal + done count instead of the
    # stale pre-reconcile values. A run absent from the registry is a no-op.
    if registry is not None:
        try:
            registry.mark_finished(rid, after, completed=completed)
        except OSError:
            pass

    if reclassified:
        ledger.event_log(
            rid, "", "info", "reconcile",
            f"reconciled {len(reclassified)} story(ies) to DONE; "
            f"run {rid[:8]} {before} → {after}",
        )
    else:
        ledger.event_log(rid, "", "info", "reconcile", "nothing to reconcile")

    return ReconcileResult(
        run_id=rid,
        reclassified=reclassified,
        run_status_before=before,
        run_status_after=after,
        fetched=fetched,
    )
