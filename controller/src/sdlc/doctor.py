# ABOUTME: `sdlc doctor` — read-side health-check across install/ledger/runs/config/deps.
# ABOUTME: Story 15.1-001. Each finding carries a CLEAN/WARN/FAIL status and a remedy.

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sdlc.build import _MIGRATIONS, Ledger, status_snapshot
from sdlc.ledger_view import default_db_path
from sdlc.registry import Registry, derive_state

__all__ = [
    "MANAGED_PATHS",
    "DEPENDENCIES",
    "Finding",
    "DoctorReport",
    "check_model_coverage",
    "check_usage_agreement",
    "run_doctor",
    "worst_status",
]

# The framework's managed install artifacts, mirroring `install/core.sh`'s
# --core symlink set. `doctor` checks each of these exists (and, when a symlink,
# resolves) under ~/.claude — exactly what `install.sh --core` would restore.
MANAGED_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "agents",
    "commands",
    "settings.json",
    "statusline-command.sh",
    "keybindings.json",
    "reference-docs",
    "docs",
    "skills",
    "hooks",
    "plugins/marketplaces/fx-claude-config",
)

# External tools the framework shells out to. `gh`/`claude` drive the core
# workflow; `semgrep`/`osv-scanner` gate the security stages. A missing tool
# degrades a feature rather than breaking the install, so it is a WARN.
DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("gh", "GitHub CLI (gh)", "install gh — https://cli.github.com"),
    (
        "claude",
        "Claude Code CLI (claude)",
        "install Claude Code — https://claude.com/claude-code",
    ),
    (
        "semgrep",
        "SAST scanner (semgrep)",
        "install semgrep — `uv tool install semgrep`",
    ),
    (
        "osv-scanner",
        "dependency scanner (osv-scanner)",
        "install osv-scanner — https://google.github.io/osv-scanner",
    ),
)

# Status severity ordering: a report's overall status is the worst of its parts.
_SEVERITY = {"CLEAN": 0, "WARN": 1, "FAIL": 2}

# A run still IN_PROGRESS in the registry with no live owner and no activity for
# longer than this is reported stale (seconds). Generous so a slow stage that is
# genuinely still running is never mislabelled stuck.
_STALE_AFTER_S = 6 * 3600

# How many recent runs the ledger-vs-logs agreement check reads (Story 28.1-001).
# Bounded because the check parses every stage attempt's transcript, and those can
# be megabytes each; the most recent runs are also the ones whose logs survive
# `sdlc clean`, so a wider sweep would mostly add unverifiable rows.
_AGREEMENT_RUN_LIMIT = 5

# At most this many residual disagreements are named individually in the finding's
# detail; the rest are summarised as "+N more" so the line stays readable.
_AGREEMENT_MAX_LISTED = 5


def worst_status(statuses: list[str]) -> str:
    """The most severe status in ``statuses`` (CLEAN < WARN < FAIL; empty=CLEAN)."""
    worst = "CLEAN"
    for status in statuses:
        if _SEVERITY.get(status, 0) > _SEVERITY[worst]:
            worst = status
    return worst


@dataclass(frozen=True)
class Finding:
    """One health-check result: a status plus an actionable remedy.

    ``check`` is the category id (install/ledger/runs/config/dependency);
    ``name`` is a human label; ``remedy`` is the command or doc that fixes the
    problem, and is empty for a CLEAN finding.
    """

    check: str
    name: str
    status: str  # CLEAN | WARN | FAIL
    detail: str
    remedy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DoctorReport:
    """The aggregate of every check, with a derived overall ``status``."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        return worst_status([f.status for f in self.findings])

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_install(claude_dir: Path) -> Finding:
    """Verify the managed ~/.claude symlinks/files are present and resolvable."""
    if not claude_dir.exists():
        return Finding(
            "install",
            "Install integrity",
            "FAIL",
            f"{claude_dir} does not exist — framework not installed",
            "run: ./install.sh --core",
        )
    # A name is broken when it is absent or a dangling symlink (a symlink whose
    # target is gone): Path.exists() follows the link, so it is False for both.
    broken = [name for name in MANAGED_PATHS if not (claude_dir / name).exists()]
    if broken:
        return Finding(
            "install",
            "Install integrity",
            "FAIL",
            f"missing/broken managed paths under {claude_dir}: {', '.join(broken)}",
            "run: ./install.sh --core (restores managed symlinks)",
        )
    return Finding(
        "install",
        "Install integrity",
        "CLEAN",
        f"all {len(MANAGED_PATHS)} managed paths present under {claude_dir}",
    )


def check_ledger(db_path: Path) -> Finding:
    """Verify the ledger is readable and its schema is current (migrations applied)."""
    if not db_path.exists():
        return Finding(
            "ledger",
            "Ledger schema + integrity",
            "CLEAN",
            "no ledger yet — nothing has been built in this repo",
        )

    expected = {version for version, *_ in _MIGRATIONS}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return Finding(
            "ledger",
            "Ledger schema + integrity",
            "FAIL",
            f"ledger could not be opened: {exc}",
            "inspect/restore .sdlc-state.db (a fresh build recreates it)",
        )
    try:
        try:
            applied = {
                row[0]
                for row in conn.execute("SELECT version FROM _migrations").fetchall()
            }
        except sqlite3.DatabaseError:
            # Either the file is corrupt or it predates the migration framework
            # (no _migrations table). Probe the runs table to tell them apart.
            try:
                conn.execute("SELECT count(*) FROM runs").fetchone()
            except sqlite3.DatabaseError as exc:
                return Finding(
                    "ledger",
                    "Ledger schema + integrity",
                    "FAIL",
                    f"ledger is unreadable / corrupt: {exc}",
                    "inspect/restore .sdlc-state.db (a fresh build recreates it)",
                )
            return Finding(
                "ledger",
                "Ledger schema + integrity",
                "WARN",
                "ledger predates the migration framework (no _migrations table)",
                "any `sdlc` verb auto-migrates it on next use (Epic-12 12.2-003)",
            )
        # _migrations read; confirm the core table reads too (integrity probe).
        conn.execute("SELECT count(*) FROM runs").fetchone()
    except sqlite3.DatabaseError as exc:
        return Finding(
            "ledger",
            "Ledger schema + integrity",
            "FAIL",
            f"ledger is unreadable / corrupt: {exc}",
            "inspect/restore .sdlc-state.db (a fresh build recreates it)",
        )
    finally:
        conn.close()

    missing = expected - applied
    if missing:
        return Finding(
            "ledger",
            "Ledger schema + integrity",
            "WARN",
            f"schema behind by {len(missing)} migration(s): {sorted(missing)}",
            "any `sdlc` verb auto-migrates it on next use (Epic-12 12.2-003)",
        )
    return Finding(
        "ledger",
        "Ledger schema + integrity",
        "CLEAN",
        "schema current, ledger readable",
    )


def check_runs(
    ledger: Ledger,
    registry: Registry,
    *,
    now: datetime | None = None,
    stale_after_s: int = _STALE_AFTER_S,
) -> Finding:
    """Detect stuck/stale runs: an IN_PROGRESS run with a dead pid or no activity.

    The registry's pid logic (Epic-11 11.2-001) is authoritative for liveness: a
    run still IN_PROGRESS whose pid is gone derives ``DEAD`` (crashed). A run with
    no registry entry is checked against the ledger's last activity instead, so a
    run whose registry record was pruned still surfaces when stale.
    """
    now = now or datetime.now(timezone.utc)

    dead: list[str] = []
    live_run_ids: set[str] = set()
    for record in registry.records():
        if derive_state(record) == "DEAD":
            dead.append(record.run_id)
        elif not record.finished_at:
            live_run_ids.add(record.run_id)

    # Ledger-side stale check: the latest run still IN_PROGRESS with no registry
    # liveness signal and no recent event is likely stuck (orphaned). A corrupt
    # or unreadable ledger is already reported by check_ledger — degrade to the
    # registry-only signal here rather than crashing the whole report.
    stale: list[str] = []
    try:
        snap = status_snapshot(ledger)
    except sqlite3.DatabaseError:
        snap = {}
    run = snap.get("run")
    if run and run.get("status") == "IN_PROGRESS":
        rid = run["id"]
        if rid not in live_run_ids and rid not in dead:
            last = _last_activity(snap)
            if last is not None and (now - last).total_seconds() > stale_after_s:
                stale.append(rid)

    if dead:
        ids = ", ".join(r[:8] for r in dead)
        return Finding(
            "runs",
            "Stuck / stale runs",
            "FAIL",
            f"{len(dead)} run(s) IN_PROGRESS with a dead pid (crashed): {ids}",
            "`sdlc reconcile` to re-check against origin, then `sdlc runs --prune`",
        )
    if stale:
        ids = ", ".join(r[:8] for r in stale)
        return Finding(
            "runs",
            "Stuck / stale runs",
            "WARN",
            f"{len(stale)} run(s) IN_PROGRESS with no activity for >"
            f"{stale_after_s // 3600}h: {ids}",
            "`sdlc status` to inspect, then `sdlc resume` or `sdlc reconcile`",
        )
    return Finding(
        "runs",
        "Stuck / stale runs",
        "CLEAN",
        "no stuck or stale runs detected",
    )


def _last_activity(snap: dict) -> datetime | None:
    """The most recent event/started_at timestamp in a status snapshot, or None."""
    candidates: list[str] = []
    run = snap.get("run") or {}
    if run.get("started_at"):
        candidates.append(str(run["started_at"]))
    for event in snap.get("events") or []:
        if event.get("ts"):
            candidates.append(str(event["ts"]))
    latest: datetime | None = None
    for raw in candidates:
        parsed = _parse_ts(raw)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _parse_ts(raw: str) -> datetime | None:
    """Parse a ledger timestamp (SQLite ``CURRENT_TIMESTAMP`` or ISO) as UTC-aware."""
    text = raw.strip()
    try:
        # SQLite CURRENT_TIMESTAMP is naive UTC: "YYYY-MM-DD HH:MM:SS".
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_config(repo_root: Path) -> Finding:
    """Verify packaged JSON schemas and the managed settings.json parse cleanly."""
    bad: list[str] = []

    schemas_dir = Path(__file__).resolve().parent / "schemas"
    for schema in sorted(schemas_dir.glob("*.json")):
        try:
            json.loads(schema.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad.append(schema.name)

    settings = repo_root / "settings.json"
    if settings.is_file():
        try:
            json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad.append("settings.json")

    if bad:
        return Finding(
            "config",
            "Config validity",
            "FAIL",
            f"invalid JSON in: {', '.join(bad)}",
            "fix the malformed JSON (validate with `jq . <file>`)",
        )
    return Finding(
        "config",
        "Config validity",
        "CLEAN",
        "settings + packaged schemas parse",
    )


def check_usage_agreement(
    db_path: Path,
    *,
    run_limit: int = _AGREEMENT_RUN_LIMIT,
    logs_root: Path | None = None,
) -> Finding:
    """Score the ledger's per-stage usage against the session logs (Story 28.1-001).

    The ledger is the cost record the estimator trains on, so silent drift between
    it and the transcripts that actually ran is a calibration bug, not a cosmetic
    one. This reports the **agreement rate** — the share of *verifiable* stage
    attempts whose recorded usage matches its log's ground truth within tolerance
    — plus every residual disagreement and its reason, so drift shows up on every
    health check rather than only when someone goes looking.

    Read-only, like every other doctor check: divergence is *reported*, and
    ``sdlc usage-reconcile`` is the verb that fixes it. Attempts whose transcript
    was pruned (or which never carried usage) are counted as **unverifiable** and
    excluded from the rate, so a repo with no logs left reports "unknown" instead
    of a vacuous 100%.
    """
    from sdlc.usage_reconcile import (
        LOG_RECOVERED,
        NO_LOG,
        NO_USAGE,
        STILL_DIVERGENT,
        reconcile_usage,
    )

    name = "Ledger-vs-logs usage agreement"
    if not db_path.exists():
        return Finding(
            "usage", name, "CLEAN", "no ledger yet — no stage usage to verify"
        )

    try:
        audit = reconcile_usage(
            Ledger(db_path), run_limit=run_limit, logs_root=logs_root, apply=False
        )
    except sqlite3.DatabaseError as exc:
        # A corrupt/unreadable ledger is already a FAIL from check_ledger; do not
        # double-report it as a usage problem.
        return Finding(
            "usage", name, "WARN",
            f"usage agreement could not be computed: {exc}",
            "fix the ledger first (see the ledger finding above)",
        )

    if not audit.audits:
        return Finding(
            "usage", name, "CLEAN", "no completed stage attempts to verify"
        )

    counts = audit.counts()
    residual_counts = ", ".join(
        f"{reason}={counts[reason]}"
        for reason in (STILL_DIVERGENT, LOG_RECOVERED, NO_LOG, NO_USAGE)
        if counts.get(reason)
    )

    scope = f"over the last {len(audit.run_ids)} run(s)"
    if audit.agreement_rate is None:
        detail = (
            f"unverifiable {scope}: none of the {len(audit.audits)} stage attempt(s) "
            "has a readable session log (transcripts pruned)"
        )
    else:
        detail = (
            f"agreement {audit.matched}/{audit.verifiable} "
            f"({audit.agreement_rate:.0%}) {scope}"
        )
        if audit.unverifiable:
            detail += f"; {audit.unverifiable} unverifiable (no readable session log)"
    if residual_counts:
        detail += f"; residual: {residual_counts}"
        listed = audit.residual[:_AGREEMENT_MAX_LISTED]
        detail += " — " + ", ".join(f"{a.label} ({a.reason})" for a in listed)
        if len(audit.residual) > len(listed):
            detail += f", +{len(audit.residual) - len(listed)} more"

    if counts.get(STILL_DIVERGENT):
        return Finding(
            "usage", name, "WARN", detail,
            "`sdlc usage-reconcile --all` to backfill usage from the session logs",
        )
    return Finding("usage", name, "CLEAN", detail)


def check_model_coverage(
    db_path: Path,
    *,
    run_limit: int = _AGREEMENT_RUN_LIMIT,
    logs_root: Path | None = None,
) -> Finding:
    """Score how much of ``stages.model`` is populated (Story 28.1-002).

    Model attribution is what makes cost-by-model a *fact* in the ledger instead
    of something re-derived by parsing logs, so a NULL there is a measurement
    gap. This reports the share of **dispatched** stage attempts (SKIPPED and
    still-running rows never ran an agent, so they owe no model) carrying one,
    across the same recent-run window as the usage-agreement check.

    Severity follows what the NULL *means*:

    * **FAIL** — a ``DONE`` attempt on the most recent run has no model *and its
      own transcript names one*. The recording had the model in hand and dropped
      it, so that is a live regression (AC3).
    * **WARN** — older or non-DONE NULLs, and DONE NULLs with no recoverable
      model: a genuine history gap, fixable with ``sdlc model-backfill`` where
      the transcript survives.
    * **CLEAN** — every dispatched attempt is attributed.

    Read-only, like every other doctor check: rows are *reported*, never coerced
    to a placeholder, so the column's true coverage is what is shown.
    """
    from sdlc.model_backfill import (
        RECOVERABLE,
        UNRECOVERABLE,
        VERIFIED_STATUSES,
        backfill_models,
    )

    name = "Per-stage model attribution"
    if not db_path.exists():
        return Finding(
            "model", name, "CLEAN", "no ledger yet — no stage attributions to verify"
        )

    try:
        audit = backfill_models(
            Ledger(db_path), run_limit=run_limit, logs_root=logs_root, apply=False
        )
    except sqlite3.DatabaseError as exc:
        # A corrupt/unreadable ledger is already a FAIL from check_ledger.
        return Finding(
            "model", name, "WARN",
            f"model coverage could not be computed: {exc}",
            "fix the ledger first (see the ledger finding above)",
        )

    if audit.coverage is None:
        return Finding(
            "model", name, "CLEAN",
            "no dispatched stage attempts to attribute",
        )

    scope = f"over the last {len(audit.run_ids)} run(s)"
    detail = (
        f"model recorded on {audit.populated}/{audit.dispatched} dispatched stage "
        f"attempt(s) ({audit.coverage:.0%}) {scope}"
    )
    counts = audit.counts()
    residual_counts = ", ".join(
        f"{reason}={counts[reason]}"
        for reason in (UNRECOVERABLE, RECOVERABLE)
        if counts.get(reason)
    )
    if residual_counts:
        detail += f"; residual: {residual_counts}"
        listed = audit.residual[:_AGREEMENT_MAX_LISTED]
        detail += " — " + ", ".join(f"{a.label} ({a.reason})" for a in listed)
        if len(audit.residual) > len(listed):
            detail += f", +{len(audit.residual) - len(listed)} more"

    remedy = "`sdlc model-backfill --all` to backfill model from the session logs"
    # The most recent run is the "fresh run" the AC guards: a completed stage
    # there with no model means the live recording path regressed, not that
    # history is merely thin.
    #
    # Narrowed to RECOVERABLE rows — the transcript proves a model *was*
    # available and the recording dropped it. An UNRECOVERABLE DONE row cannot
    # be a recording regression: not every DONE row dispatched an agent.
    # `reconcile_run` (which runs on every close-out, not just the standalone
    # verb) synthesizes DONE build/coverage/review/merge rows for a
    # parked-then-landed story, and a plain-text `SDLC_AGENT_CMD` harness names
    # no model at all. FAILing those would assert a regression that never
    # happened and print a remedy `model-backfill` cannot apply — a permanent
    # `sdlc doctor --exit-code` 2. They stay a WARN, counted, never coerced.
    fresh = audit.run_ids[0] if audit.run_ids else None
    regressions = [
        a
        for a in (audit.nulls_in(fresh, VERIFIED_STATUSES) if fresh else [])
        if a.reason == RECOVERABLE
    ]
    if regressions:
        return Finding(
            "model", name, "FAIL",
            detail + (
                f"; regression: {len(regressions)} completed stage(s) on the latest "
                "run recorded no model"
            ),
            remedy,
        )
    if audit.residual:
        return Finding("model", name, "WARN", detail, remedy)
    return Finding("model", name, "CLEAN", detail)


def _default_dep_probe(tool: str) -> bool:
    """True when ``tool`` is on PATH and answers ``--version`` with exit 0.

    A missing binary short-circuits via ``shutil.which`` (no subprocess). A
    present-but-broken binary (non-zero ``--version``) reports False so doctor
    flags it rather than silently trusting it.
    """
    if shutil.which(tool) is None:
        return False
    try:
        proc = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check_dependencies(probe: Callable[[str], bool]) -> list[Finding]:
    """One finding per external dependency: CLEAN when present, WARN when absent."""
    findings: list[Finding] = []
    for tool, label, remedy in DEPENDENCIES:
        if probe(tool):
            findings.append(Finding("dependency", label, "CLEAN", f"{tool} available"))
        else:
            findings.append(
                Finding(
                    "dependency",
                    label,
                    "WARN",
                    f"{tool} not found (a feature that uses it will be unavailable)",
                    remedy,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _detect_repo_root() -> Path:
    """The config repo root: the git toplevel of cwd, else the package's ancestor."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    # doctor.py -> sdlc -> src -> controller -> <config repo root>
    return Path(__file__).resolve().parents[3]


def run_doctor(
    *,
    repo_root: Path | None = None,
    claude_dir: Path | None = None,
    db_path: Path | None = None,
    registry: Registry | None = None,
    dep_probe: Callable[[str], bool] | None = None,
    now: datetime | None = None,
    stale_after_s: int = _STALE_AFTER_S,
) -> DoctorReport:
    """Run every health-check and return the aggregated :class:`DoctorReport`.

    Read-only: doctor never mutates the ledger or the install — a behind-on-
    migrations DB is *reported* (Epic-12 12.2-003 fixes it), not migrated here.
    All inputs are injectable so the checks are testable against seeded-broken
    fixtures.
    """
    repo_root = repo_root or _detect_repo_root()
    claude_dir = claude_dir or (Path.home() / ".claude")
    db_path = db_path or default_db_path()
    registry = registry or Registry()
    dep_probe = dep_probe or _default_dep_probe

    findings: list[Finding] = [
        check_install(claude_dir),
        check_ledger(db_path),
        check_runs(Ledger(db_path), registry, now=now, stale_after_s=stale_after_s),
        check_config(repo_root),
        check_usage_agreement(db_path),
        check_model_coverage(db_path),
    ]
    findings.extend(check_dependencies(dep_probe))
    return DoctorReport(findings=findings)
