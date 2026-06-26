# ABOUTME: Read-side helpers for `sdlc state` — a greppable state-machine dump.
# ABOUTME: Story 10.1-001 (state); Story 15.1-002 (portable markdown handoff).

from __future__ import annotations

from pathlib import Path

from sdlc.build import Ledger

__all__ = ["state_report", "format_state", "format_markdown"]


def state_report(ledger: Ledger, run_id: str) -> list[dict]:
    """The persisted state-machine rows for ``run_id`` (one dict per stage row).

    A thin pass-through to :meth:`Ledger.state_rows` so the CLI and any future
    consumer share one shape: ``{story_id, stage_name, status, attempt, branch,
    pr_number, harness}``.
    """
    return ledger.state_rows(run_id)


def format_state(rows: list[dict]) -> list[str]:
    """Render state rows as fixed-width, greppable lines (header first).

    The columns are stable so ``sdlc state | grep <story>`` and column-based
    tooling stay reliable. A missing branch falls back to the deterministic
    ``feature/<story_id>`` the build state machine uses; a missing PR renders
    as ``-``. The ``HARNESS`` column (Story 20.2-002) shows which harness ran
    each stage, defaulting to ``claude`` for rows that predate harness routing.
    """
    lines = [
        f"{'STORY':<16}{'STAGE':<11}{'STATUS':<13}{'ATT':<5}"
        f"{'HARNESS':<9}{'PR':<7}BRANCH"
    ]
    for r in rows:
        pr = r.get("pr_number")
        pr_disp = f"#{pr}" if pr else "-"
        branch = r.get("branch") or f"feature/{r.get('story_id', '?')}"
        lines.append(
            f"{str(r.get('story_id', '?')):<16}"
            f"{str(r.get('stage_name', '?')):<11}"
            f"{str(r.get('status', '?')):<13}"
            f"{str(r.get('attempt', '?')):<5}"
            f"{str(r.get('harness') or 'claude'):<9}"
            f"{pr_disp:<7}{branch}"
        )
    return lines


# ---------------------------------------------------------------------------
# Story 15.1-002: portable markdown handoff (`sdlc status --markdown`)
# ---------------------------------------------------------------------------


def _scrub(text: str, home: Path) -> str:
    """Redact the user's home directory from ``text`` so the export leaks no path.

    The only PII the snapshot/doctor strings carry is an absolute home path (it
    embeds the username, e.g. ``/Users/fxmartin/.claude``). Replacing it with
    ``~`` keeps the report self-contained and shareable. No tokens flow through
    these strings, so this is the only redaction needed.
    """
    return text.replace(str(home), "~")


def format_markdown(
    snapshot: dict,
    doctor: dict,
    *,
    home: Path | None = None,
) -> str:
    """Render a portable, secret-free markdown handoff from a snapshot + doctor report.

    ``snapshot`` is the shape from :func:`sdlc.build.status_snapshot`; ``doctor``
    is :meth:`sdlc.doctor.DoctorReport.to_dict`. The output covers readiness (the
    doctor summary), install health, the active/recent run and its stages, and
    pending governance events (stories parked ``AWAITING_APPROVAL`` at the
    risk-gate). Home paths are scrubbed to ``~`` so the report can be pasted into
    an issue or chat without leaking a username.
    """
    home = home or Path.home()
    findings = doctor.get("findings", [])
    overall = doctor.get("status", "?")

    lines: list[str] = [
        "# SDLC Status Report",
        "",
        "_Portable handoff — paste into an issue or chat when asking for help._",
        "",
    ]

    # --- Readiness (doctor summary) ----------------------------------------
    lines.append("## Readiness")
    lines.append("")
    lines.append(f"**Overall: {overall}**")
    lines.append("")
    lines.append("| Check | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for f in findings:
        detail = f.get("detail", "").replace("|", "\\|")
        lines.append(
            f"| {f.get('name', '?')} | {f.get('status', '?')} | {detail} |"
        )
    lines.append("")
    remedies = [f for f in findings if f.get("remedy")]
    if remedies:
        lines.append("**Remedies:**")
        lines.append("")
        for f in remedies:
            lines.append(f"- **{f.get('name', '?')}**: {f.get('remedy', '')}")
        lines.append("")

    # --- Install health ----------------------------------------------------
    lines.append("## Install health")
    lines.append("")
    install = next((f for f in findings if f.get("check") == "install"), None)
    if install is not None:
        lines.append(
            f"**{install.get('status', '?')}** — {install.get('detail', '')}"
        )
        if install.get("remedy"):
            lines.append("")
            lines.append(f"Remedy: {install['remedy']}")
    else:
        lines.append("_No install check available._")
    lines.append("")

    # --- Active / recent run + stages --------------------------------------
    lines.append("## Run")
    lines.append("")
    run = snapshot.get("run")
    if not run:
        lines.append("_No active or recent run in this repo's ledger._")
        lines.append("")
    else:
        counts = snapshot.get("counts", {})
        run_id = str(run.get("id", "?"))
        lines.append(
            f"**run `{run_id[:8]}` — {run.get('status', '?')}** "
            f"(scope={run.get('scope', '?')}, mode={run.get('mode', '?')})"
        )
        lines.append("")
        lines.append(
            f"{counts.get('done', 0)}/{counts.get('total', 0)} done, "
            f"{counts.get('failed', 0)} failed, {counts.get('blocked', 0)} blocked, "
            f"{counts.get('in_progress', 0)} in progress, "
            f"{counts.get('awaiting_approval', 0)} awaiting approval"
        )
        lines.append("")
        stories = snapshot.get("stories", [])
        if stories:
            lines.append("| Story | Status | Stage | PR |")
            lines.append("| --- | --- | --- | --- |")
            for s in stories:
                stage = s.get("current_stage") or "-"
                pr = s.get("pr_number")
                pr_disp = f"#{pr}" if pr else "-"
                lines.append(
                    f"| {s.get('story_id', '?')} | {s.get('status', '?')} "
                    f"| {stage} | {pr_disp} |"
                )
            lines.append("")

    # --- Pending governance events (risk-gate approvals) -------------------
    lines.append("## Pending approvals")
    lines.append("")
    stories = snapshot.get("stories", []) if run else []
    pending = [s for s in stories if s.get("status") == "AWAITING_APPROVAL"]
    if pending:
        lines.append("Stories parked at the risk-gate, awaiting a human merge decision:")
        lines.append("")
        lines.append("| Story | Stage | PR |")
        lines.append("| --- | --- | --- |")
        for s in pending:
            stage = s.get("current_stage") or "-"
            pr = s.get("pr_number")
            pr_disp = f"#{pr}" if pr else "-"
            lines.append(f"| {s.get('story_id', '?')} | {stage} | {pr_disp} |")
        lines.append("")
    else:
        lines.append("None.")
        lines.append("")

    return _scrub("\n".join(lines).rstrip() + "\n", home)
