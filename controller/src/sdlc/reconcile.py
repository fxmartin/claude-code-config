# ABOUTME: Close-out reconciliation — verify parked stories actually landed on
# ABOUTME: origin/main before the run terminal is computed (Story 12.3-001).

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdlc.build import Ledger, _base_ref, _git

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


def _rev(root: Path, ref: str) -> str:
    """The resolved SHA of ``ref``, or "" when it cannot be resolved."""
    out = _git(root, "rev-parse", "--verify", "--quiet", ref)
    return out.stdout.strip() if out.returncode == 0 else ""


def _detect_landing(
    story_id: str, pr_number: int | None, base: str | None, root: Path
) -> tuple[str, str] | None:
    """Whether ``feature/<story_id>``'s work is present on ``base`` / merged.

    Returns ``(signal, sha)`` for the first matching detector, else None. The
    detectors are deliberately complementary across merge styles:

    - ``is-ancestor`` — fast-forward / merge-commit landings (branch tip is an
      ancestor of base).
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
        if _git(root, "merge-base", "--is-ancestor", branch, base).returncode == 0:
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
        _ensure_merge_done(ledger, rid, sid, breakdown.get(sid, []))
        ledger.event_log(
            rid, sid, "info", "reconcile",
            f"reconciled {r.get('status')} → DONE via {signal}: "
            f"feature/{sid} landed on {base or 'base'}"
            + (f" (merge {sha[:8]})" if sha else ""),
        )
        reclassified.append(
            {"story_id": sid, "from_status": r.get("status"), "signal": signal, "sha": sha}
        )

    statuses = {row["story_id"]: row["status"] for row in ledger.story_rows(rid)}
    after = _compute_terminal(statuses)
    completed = sum(1 for v in statuses.values() if v == "DONE")
    failed = sum(1 for v in statuses.values() if v == "FAILED")
    ledger.run_update_counts(rid, completed, failed)
    ledger.run_update_status(rid, after)

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
