# ABOUTME: Tests for `sdlc doctor` — health-check across install/ledger/runs/config/deps.
# ABOUTME: Story 15.1-001. Seeds broken fixtures (missing dep, stale ledger, stuck run).

from __future__ import annotations

import json
import plistlib
import socket
import sqlite3
import subprocess
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app
from sdlc.doctor import (
    MANAGED_PATHS,
    DoctorReport,
    Finding,
    check_controller_version,
    check_harness_pin,
    check_model_coverage,
    check_queue_service,
    check_usage_agreement,
    run_doctor,
    worst_status,
)
from sdlc.model_backfill import backfill_models
from sdlc.registry import Registry, RunRecord

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_queue_service(monkeypatch, tmp_path):
    """Keep `run_doctor` off the developer's real LaunchAgent plist (35.1-003).

    On home-lab the plist exists and the service is up; anywhere else it is
    absent. Either way an un-isolated run would make these tests host-dependent.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


# --- fixtures ---------------------------------------------------------------


def _healthy_install(tmp_path: Path) -> tuple[Path, Path]:
    """Build a healthy ~/.claude install symlinked into a repo root.

    Returns ``(claude_dir, repo_root)``.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # A valid settings.json so the config check passes.
    (repo_root / "settings.json").write_text("{}\n", encoding="utf-8")
    # A harness pin, so the healthy fixture is healthy under the #551 follow-up
    # check too: an unpinned repo follows whatever registry a reinstall leaves
    # behind, which is a WARN, not a clean bill of health.
    (repo_root / ".sdlc-harness.yaml").write_text(
        "harness:\n  default: claude\n", encoding="utf-8"
    )
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    for name in MANAGED_PATHS:
        target = repo_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # A file for *.json/*.sh/*.md names, a directory otherwise — only
        # existence + a resolvable symlink matters to the install check.
        if name.endswith((".json", ".sh", ".md")):
            target.write_text("{}\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
        link = claude_dir / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return claude_dir, repo_root


def _fresh_ledger(tmp_path: Path) -> Path:
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).init()
    return db


def _all_present_probe(_tool: str) -> bool:
    return True


def _doctor(tmp_path: Path, **overrides) -> DoctorReport:
    """run_doctor with a fully healthy default fixture, overridable per test."""
    claude_dir, repo_root = _healthy_install(tmp_path)
    kwargs = dict(
        repo_root=repo_root,
        claude_dir=claude_dir,
        registry=Registry(path=tmp_path / "registry.json"),
        dep_probe=_all_present_probe,
    )
    kwargs.update(overrides)
    # Only seed the default ledger when the test did not supply its own — seeding
    # at the shared default path would otherwise re-migrate an overridden DB
    # (and setdefault would build it eagerly even when unused).
    if "db_path" not in kwargs:
        kwargs["db_path"] = _fresh_ledger(tmp_path)
    return run_doctor(**kwargs)


def _finding(report: DoctorReport, check: str) -> Finding:
    return next(f for f in report.findings if f.check == check)


# --- worst_status -----------------------------------------------------------


def test_worst_status_orders_clean_warn_fail() -> None:
    assert worst_status(["CLEAN", "CLEAN"]) == "CLEAN"
    assert worst_status(["CLEAN", "WARN"]) == "WARN"
    assert worst_status(["WARN", "FAIL", "CLEAN"]) == "FAIL"
    assert worst_status([]) == "CLEAN"


# --- all clean --------------------------------------------------------------


def test_run_doctor_all_clean(tmp_path: Path) -> None:
    report = _doctor(tmp_path)
    assert report.status == "CLEAN"
    # Every check category present.
    checks = {f.check for f in report.findings}
    assert {"install", "ledger", "runs", "config"} <= checks
    # A dependency finding per tool (gh, claude, semgrep, osv-scanner, glab).
    assert sum(1 for f in report.findings if f.check == "dependency") == 5
    # CLEAN findings carry no remedy noise.
    assert all(f.remedy == "" for f in report.findings if f.status == "CLEAN")


# --- install integrity ------------------------------------------------------


def test_install_missing_symlink_fails(tmp_path: Path) -> None:
    claude_dir, repo_root = _healthy_install(tmp_path)
    (claude_dir / "hooks").unlink()  # drift: remove a managed symlink
    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=_fresh_ledger(tmp_path),
        registry=Registry(path=tmp_path / "registry.json"),
        dep_probe=_all_present_probe,
    )
    install = _finding(report, "install")
    assert install.status == "FAIL"
    assert "hooks" in install.detail
    assert "install.sh" in install.remedy


def test_install_broken_symlink_fails(tmp_path: Path) -> None:
    claude_dir, repo_root = _healthy_install(tmp_path)
    (repo_root / "agents").rmdir()  # dangling symlink: target gone
    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=_fresh_ledger(tmp_path),
        registry=Registry(path=tmp_path / "registry.json"),
        dep_probe=_all_present_probe,
    )
    install = _finding(report, "install")
    assert install.status == "FAIL"
    assert "agents" in install.detail


def test_install_not_installed_fails(tmp_path: Path) -> None:
    report = run_doctor(
        repo_root=tmp_path / "repo",
        claude_dir=tmp_path / "nonexistent-claude",
        db_path=_fresh_ledger(tmp_path),
        registry=Registry(path=tmp_path / "registry.json"),
        dep_probe=_all_present_probe,
    )
    install = _finding(report, "install")
    assert install.status == "FAIL"
    assert "install.sh" in install.remedy


# --- ledger schema + integrity ---------------------------------------------


def test_ledger_absent_is_clean(tmp_path: Path) -> None:
    report = _doctor(tmp_path, db_path=tmp_path / "missing.db")
    assert _finding(report, "ledger").status == "CLEAN"


def test_ledger_stale_schema_warns(tmp_path: Path) -> None:
    db = _fresh_ledger(tmp_path)
    # Simulate a ledger behind on migrations: drop the newest applied version.
    with sqlite3.connect(db) as conn:
        newest = conn.execute("SELECT MAX(version) FROM _migrations").fetchone()[0]
        conn.execute("DELETE FROM _migrations WHERE version = ?", (newest,))
    report = _doctor(tmp_path, db_path=db)
    ledger = _finding(report, "ledger")
    assert ledger.status == "WARN"
    assert ledger.remedy  # an actionable migrate remedy


def test_ledger_pre_migration_framework_warns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    # A ledger that predates the migration framework: has runs, no _migrations.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT)")
    report = _doctor(tmp_path, db_path=db)
    assert _finding(report, "ledger").status == "WARN"


def test_ledger_renumbered_migration_with_stale_name_warns(tmp_path: Path) -> None:
    """Issue #621: a ledger bootstrapped before a migration renumbering can
    hold a bookkeeping row whose version matches migration 1 but whose name
    is the old ``'init'`` identity rather than "stage usage columns". A
    version-only comparison would call this schema current (false CLEAN);
    the name-aware check must flag it as behind instead."""
    db = _fresh_ledger(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE _migrations SET name = 'init' WHERE version = 1")
    report = _doctor(tmp_path, db_path=db)
    ledger = _finding(report, "ledger")
    assert ledger.status == "WARN"
    assert "1" in ledger.detail
    assert ledger.remedy


def test_ledger_that_cannot_be_opened_fails(tmp_path: Path) -> None:
    """A path that exists but is not an openable database is a FAIL, not a crash."""
    db = tmp_path / "ledger-is-a-directory.db"
    db.mkdir()
    report = _doctor(tmp_path, db_path=db)
    ledger = _finding(report, "ledger")
    assert ledger.status == "FAIL"
    assert "could not be opened" in ledger.detail
    assert ledger.remedy


# --- queue presence + schema currency + job counts (Story 32.1-001) --------


def test_queue_absent_is_clean(tmp_path: Path) -> None:
    report = _doctor(tmp_path, queue_path=tmp_path / "missing-queue.db")
    queue = _finding(report, "queue")
    assert queue.status == "CLEAN"
    assert "no" in queue.detail.lower()


def test_queue_present_reports_job_counts(tmp_path: Path) -> None:
    from sdlc.queue import QueueStore

    queue_path = tmp_path / "queue.db"
    store = QueueStore(queue_path)
    store.init()
    store.add_job(repo="/repo-a", kind="build", scope="epic-1")
    job_id = store.add_job(repo="/repo-b", kind="fix", scope="7")
    store.cancel_job(job_id)

    report = _doctor(tmp_path, queue_path=queue_path)
    queue = _finding(report, "queue")
    assert queue.status == "CLEAN"
    assert "1 queued" in queue.detail
    assert "1 cancelled" in queue.detail


def test_queue_that_cannot_be_opened_fails(tmp_path: Path) -> None:
    """A path that exists but is not an openable database is a FAIL, not a crash."""
    queue_path = tmp_path / "queue-is-a-directory.db"
    queue_path.mkdir()
    report = _doctor(tmp_path, queue_path=queue_path)
    queue = _finding(report, "queue")
    assert queue.status == "FAIL"
    assert "could not be opened" in queue.detail
    assert queue.remedy


def test_queue_pre_migration_framework_warns(tmp_path: Path) -> None:
    queue_path = tmp_path / "old-queue.db"
    # A queue that predates the migration framework: has jobs, no _migrations.
    with sqlite3.connect(queue_path) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, state TEXT)")
    report = _doctor(tmp_path, queue_path=queue_path)
    assert _finding(report, "queue").status == "WARN"


def test_queue_corrupt_fails(tmp_path: Path) -> None:
    queue_path = tmp_path / "corrupt-queue.db"
    queue_path.write_bytes(b"this is not a sqlite database at all")
    report = _doctor(tmp_path, queue_path=queue_path)
    assert _finding(report, "queue").status == "FAIL"


def test_queue_missing_jobs_table_fails(tmp_path: Path) -> None:
    """`_migrations` exists but `jobs` does not — the count query itself fails."""
    queue_path = tmp_path / "half-migrated-queue.db"
    with sqlite3.connect(queue_path) as conn:
        conn.execute(
            "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT, "
            "applied_at TIMESTAMP)"
        )
    report = _doctor(tmp_path, queue_path=queue_path)
    queue = _finding(report, "queue")
    assert queue.status == "FAIL"
    assert "unreadable" in queue.detail.lower() or "corrupt" in queue.detail.lower()


def test_queue_stale_schema_warns(tmp_path: Path, monkeypatch) -> None:
    from sdlc.queue import QueueStore

    import sdlc.doctor as doctor_mod

    queue_path = tmp_path / "queue.db"
    QueueStore(queue_path).init()
    # Simulate a migration newer than anything `_MIGRATIONS` ships today, so
    # the on-disk queue is genuinely behind the code's expectations.
    monkeypatch.setattr(
        doctor_mod, "_QUEUE_MIGRATIONS", [(999, "future_migration", "jobs", [], None)]
    )

    report = _doctor(tmp_path, queue_path=queue_path)
    queue = _finding(report, "queue")
    assert queue.status == "WARN"
    assert queue.remedy


def test_queue_renumbered_migration_with_stale_name_warns(tmp_path: Path) -> None:
    """Mirrors test_ledger_renumbered_migration_with_stale_name_warns for the
    queue's own _MIGRATIONS/_apply_migrations mirror (Issue #621)."""
    from sdlc.queue import QueueStore

    queue_path = tmp_path / "queue.db"
    QueueStore(queue_path).init()
    with sqlite3.connect(queue_path) as conn:
        newest_name = conn.execute(
            "SELECT name FROM _migrations WHERE version = 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE _migrations SET name = ? WHERE version = 1", (f"stale-{newest_name}",)
        )

    report = _doctor(tmp_path, queue_path=queue_path)
    queue = _finding(report, "queue")
    assert queue.status == "WARN"
    assert queue.remedy


def test_ledger_corrupt_fails(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database at all")
    report = _doctor(tmp_path, db_path=db)
    ledger = _finding(report, "ledger")
    assert ledger.status == "FAIL"


# --- stuck / stale runs -----------------------------------------------------


def test_runs_clean_when_none_in_progress(tmp_path: Path) -> None:
    report = _doctor(tmp_path)
    assert _finding(report, "runs").status == "CLEAN"


def test_stuck_run_dead_pid_fails(tmp_path: Path) -> None:
    reg_path = tmp_path / "registry.json"
    registry = Registry(path=reg_path)
    # An IN_PROGRESS run whose pid is long dead → derives DEAD.
    registry.register(
        RunRecord(
            run_id="run-dead",
            repo=str(tmp_path),
            db=str(tmp_path / ".sdlc-state.db"),
            scope="all",
            pid=2_147_483_646,  # not a live pid
            status="IN_PROGRESS",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    report = _doctor(tmp_path, registry=registry)
    runs = _finding(report, "runs")
    assert runs.status == "FAIL"
    assert "run-dead"[:8] in runs.detail
    assert runs.remedy  # points at resume/reconcile/prune


def test_stale_in_progress_run_warns(tmp_path: Path) -> None:
    """A run left IN_PROGRESS with no registry entry and no recent activity is stale.

    This is the orphan shape the usage reconciliation has to cope with too: the
    registry record was pruned, so liveness can only be inferred from the
    ledger's own last activity.
    """
    db = _fresh_ledger(tmp_path)
    Ledger(db).run_create("epic-28", "auto")  # IN_PROGRESS, unknown to the registry

    report = _doctor(
        tmp_path,
        db_path=db,
        now=datetime.now(timezone.utc) + timedelta(hours=48),
    )

    runs = _finding(report, "runs")
    assert runs.status == "WARN"
    assert "no activity" in runs.detail
    assert runs.remedy  # points at status/resume/reconcile


def test_run_with_an_unparseable_timestamp_is_not_reported_stale(tmp_path: Path) -> None:
    """An unreadable `started_at` yields no staleness verdict, not a false one."""
    db = _fresh_ledger(tmp_path)
    ledger = Ledger(db)
    run_id = ledger.run_create("epic-28", "auto")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE runs SET started_at = 'not-a-timestamp' WHERE id = ?", (run_id,)
        )

    report = _doctor(
        tmp_path,
        db_path=db,
        now=datetime.now(timezone.utc) + timedelta(hours=48),
    )

    assert _finding(report, "runs").status == "CLEAN"


def test_live_run_is_clean(tmp_path: Path) -> None:
    import os

    registry = Registry(path=tmp_path / "registry.json")
    registry.register(
        RunRecord(
            run_id="run-live",
            repo=str(tmp_path),
            db=str(tmp_path / ".sdlc-state.db"),
            scope="all",
            pid=os.getpid(),  # this very process → alive
            status="IN_PROGRESS",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    report = _doctor(tmp_path, registry=registry)
    assert _finding(report, "runs").status == "CLEAN"


# --- config validity --------------------------------------------------------


def test_config_invalid_settings_json_fails(tmp_path: Path) -> None:
    claude_dir, repo_root = _healthy_install(tmp_path)
    (repo_root / "settings.json").write_text("{ not valid json", encoding="utf-8")
    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=_fresh_ledger(tmp_path),
        registry=Registry(path=tmp_path / "registry.json"),
        dep_probe=_all_present_probe,
    )
    config = _finding(report, "config")
    assert config.status == "FAIL"
    assert "settings.json" in config.detail


# --- dependencies -----------------------------------------------------------


def test_dependency_missing_warns(tmp_path: Path) -> None:
    def probe(tool: str) -> bool:
        return tool != "osv-scanner"

    report = _doctor(tmp_path, dep_probe=probe)
    osv = next(
        f
        for f in report.findings
        if f.check == "dependency" and "osv-scanner" in f.name
    )
    assert osv.status == "WARN"
    assert osv.remedy
    # Other deps remain CLEAN.
    others = [
        f
        for f in report.findings
        if f.check == "dependency" and "osv-scanner" not in f.name
    ]
    assert all(f.status == "CLEAN" for f in others)


# --- glab host-aware dependency check (issue #599 / 23.6-002 follow-through) -


def test_run_doctor_warns_when_glab_missing_and_host_undetectable(
    tmp_path: Path,
) -> None:
    """Issue #599 regression: a repo with no glab must never report fully CLEAN.

    `_healthy_install` builds a plain directory with no git remote, so the host
    is undetectable — doctor must still surface the gap instead of staying
    silent about it (the WARN fallback for the ambiguous case).
    """

    def probe(tool: str) -> bool:
        return tool != "glab"

    report = _doctor(tmp_path, dep_probe=probe)
    assert report.status != "CLEAN"
    glab = next(f for f in report.findings if f.check == "dependency" and "glab" in f.name)
    assert glab.status == "WARN"
    assert glab.remedy


def test_glab_dependency_clean_and_not_applicable_on_github_host(tmp_path: Path) -> None:
    from sdlc.doctor import check_glab_dependency

    finding = check_glab_dependency(tmp_path, lambda _tool: False, host="github")
    assert finding.status == "CLEAN"
    assert "not applicable" in finding.detail
    assert finding.remedy == ""


def test_glab_dependency_warns_missing_on_gitlab_host(tmp_path: Path) -> None:
    from sdlc.doctor import check_glab_dependency

    finding = check_glab_dependency(tmp_path, lambda _tool: False, host="gitlab")
    assert finding.status == "WARN"
    assert "GitLab" in finding.detail
    assert finding.remedy


def test_glab_dependency_clean_when_present_on_gitlab_host(tmp_path: Path) -> None:
    from sdlc.doctor import check_glab_dependency

    finding = check_glab_dependency(tmp_path, lambda _tool: True, host="gitlab")
    assert finding.status == "CLEAN"
    assert finding.remedy == ""


def test_glab_dependency_warns_on_undetectable_host(tmp_path: Path) -> None:
    from sdlc.doctor import check_glab_dependency

    finding = check_glab_dependency(tmp_path, lambda _tool: False, host=None)
    assert finding.status == "WARN"
    assert "undetected" in finding.detail
    assert finding.remedy


def test_glab_dependency_detects_host_from_repo_root(monkeypatch, tmp_path: Path) -> None:
    """Default host resolution reads the repo's real remote (issue_host.detect_host)."""
    from sdlc import doctor as doctor_mod
    from sdlc import issue_host as issue_host_mod

    monkeypatch.setattr(issue_host_mod, "detect_host", lambda root: issue_host_mod.GITHUB)
    finding = doctor_mod.check_glab_dependency(tmp_path, lambda _tool: False)
    assert finding.status == "CLEAN"
    assert "not applicable" in finding.detail


# --- ledger-vs-logs usage agreement (Story 28.1-001) ------------------------


def _usage_ledger(tmp_path: Path, *, log: str | None, ledger_cost: float | None) -> Path:
    """A ledger with one finished stage attempt, optionally with a session log."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-001", "epic-28", "t", "Must", 5, "python-backend-engineer",
        "feature/28.1-001", None, "DONE",
    )
    ledger.stage_start(run_id, "28.1-001", "build", 1)
    ledger.stage_finish(run_id, "28.1-001", "build", 1, "DONE")
    if ledger_cost is not None:
        ledger.stage_set_usage(
            run_id, "28.1-001", "build", 1, session_id="s",
            input_tokens=100, output_tokens=200, cache_read_tokens=0,
            cache_creation_tokens=0, cost_usd=ledger_cost,
        )
    if log is not None:
        logs_dir = Path(f"{db}.logs") / run_id
        logs_dir.mkdir(parents=True)
        (logs_dir / "28.1-001-build-1.log").write_text(log, encoding="utf-8")
    return db


_RESULT_LINE = json.dumps(
    {
        "type": "result",
        "result": "ok",
        "session_id": "s",
        "total_cost_usd": 1.5,
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }
) + "\n"

_CRASHED_LOG = json.dumps(
    {
        "type": "assistant",
        "session_id": "s",
        "message": {"content": [], "usage": {"input_tokens": 100, "output_tokens": 200}},
    }
) + "\n"


def test_usage_agreement_clean_when_ledger_matches_the_logs(tmp_path: Path) -> None:
    db = _usage_ledger(tmp_path, log=_RESULT_LINE, ledger_cost=1.5)
    finding = check_usage_agreement(db)
    assert finding.check == "usage" and finding.status == "CLEAN"
    assert "1/1" in finding.detail and "100%" in finding.detail


def test_usage_agreement_warns_and_names_the_divergent_row(tmp_path: Path) -> None:
    """A ledger figure that disagrees with the log is actionable drift."""
    db = _usage_ledger(tmp_path, log=_RESULT_LINE, ledger_cost=0.02)
    finding = check_usage_agreement(db)
    assert finding.status == "WARN"
    assert "still-divergent=1" in finding.detail
    assert "28.1-001/build#1" in finding.detail
    assert "usage-reconcile" in finding.remedy


def test_usage_agreement_lists_log_recovered_rows_as_residual(tmp_path: Path) -> None:
    """A crashed session is verifiable but cost-less, so it never counts as agreement."""
    db = _usage_ledger(tmp_path, log=_CRASHED_LOG, ledger_cost=None)
    finding = check_usage_agreement(db)
    assert "log-recovered=1" in finding.detail
    assert "0/1" in finding.detail


def test_usage_agreement_reports_pruned_logs_as_unverifiable(tmp_path: Path) -> None:
    """AC5: no transcript on disk must never read as agreement."""
    db = _usage_ledger(tmp_path, log=None, ledger_cost=1.5)
    finding = check_usage_agreement(db)
    assert finding.status == "CLEAN"
    assert "unverifiable" in finding.detail and "no-log=1" in finding.detail
    assert "100%" not in finding.detail


def test_usage_agreement_clean_without_a_ledger(tmp_path: Path) -> None:
    finding = check_usage_agreement(tmp_path / "missing.db")
    assert finding.status == "CLEAN" and "no ledger" in finding.detail


def test_usage_agreement_is_read_only(tmp_path: Path) -> None:
    """Doctor never mutates: a divergent row is reported, not backfilled."""
    db = _usage_ledger(tmp_path, log=_RESULT_LINE, ledger_cost=0.02)
    check_usage_agreement(db)
    row = Ledger(db).stage_usage_rows(Ledger(db).latest_run_id())[0]
    assert row["cost_usd"] == 0.02 and row["usage_source"] is None


def test_run_doctor_includes_the_usage_agreement_check(tmp_path: Path) -> None:
    report = _doctor(tmp_path)
    assert _finding(report, "usage").status == "CLEAN"


def _usage_ledger_rows(tmp_path: Path, *, logged: dict[str, str]) -> Path:
    """A ledger with a `build` and a `review` attempt; only `logged` keeps a log."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-001", "epic-28", "t", "Must", 5, "python-backend-engineer",
        "feature/28.1-001", None, "DONE",
    )
    logs_dir = Path(f"{db}.logs") / run_id
    logs_dir.mkdir(parents=True)
    for stage in ("build", "review"):
        ledger.stage_start(run_id, "28.1-001", stage, 1)
        ledger.stage_finish(run_id, "28.1-001", stage, 1, "DONE")
        ledger.stage_set_usage(
            run_id, "28.1-001", stage, 1, session_id="s",
            input_tokens=100, output_tokens=200, cache_read_tokens=0,
            cache_creation_tokens=0, cost_usd=1.5,
        )
    for stage, body in logged.items():
        (logs_dir / f"28.1-001-{stage}-1.log").write_text(body, encoding="utf-8")
    return db


def test_usage_agreement_reports_pruned_rows_alongside_a_real_rate(tmp_path: Path) -> None:
    """AC5: a partially-pruned repo scores only what it can actually verify.

    The rate is over the rows with a readable log; the pruned one is named as
    unverifiable rather than silently inflating the denominator (or the rate).
    """
    db = _usage_ledger_rows(tmp_path, logged={"build": _RESULT_LINE})
    finding = check_usage_agreement(db)
    assert finding.status == "CLEAN"
    assert "1/1" in finding.detail and "100%" in finding.detail
    assert "1 unverifiable" in finding.detail
    assert "no-log=1" in finding.detail


def test_usage_agreement_truncates_a_long_residual_list(tmp_path: Path) -> None:
    """Residuals stay readable: at most five are named, the rest counted."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-001", "epic-28", "t", "Must", 5, "python-backend-engineer",
        "feature/28.1-001", None, "DONE",
    )
    for attempt in range(1, 8):  # seven pruned attempts
        ledger.stage_start(run_id, "28.1-001", "build", attempt)
        ledger.stage_finish(run_id, "28.1-001", "build", attempt, "DONE")

    finding = check_usage_agreement(db)

    assert "no-log=7" in finding.detail
    assert finding.detail.count("28.1-001/build#") == 5
    assert "+2 more" in finding.detail


# --- report serialization ---------------------------------------------------


def test_report_to_dict_shape(tmp_path: Path) -> None:
    report = _doctor(tmp_path)
    data = report.to_dict()
    assert data["status"] == "CLEAN"
    assert isinstance(data["findings"], list)
    assert {"check", "name", "status", "detail", "remedy"} <= set(data["findings"][0])


# --- CLI --------------------------------------------------------------------


def test_cli_doctor_exits_zero_without_exit_code_flag(tmp_path: Path) -> None:
    """A broken install still exits 0 by default — doctor is a safe report."""
    db = _fresh_ledger(tmp_path)
    result = runner.invoke(
        app,
        [
            "doctor",
            "--db",
            str(db),
            "--claude-dir",
            str(tmp_path / "nonexistent-claude"),
        ],
    )
    assert result.exit_code == 0
    assert "FAIL" in result.stdout


def test_cli_doctor_exit_code_flag_nonzero_on_fail(tmp_path: Path) -> None:
    db = _fresh_ledger(tmp_path)
    result = runner.invoke(
        app,
        [
            "doctor",
            "--db",
            str(db),
            "--claude-dir",
            str(tmp_path / "nonexistent-claude"),
            "--exit-code",
        ],
    )
    assert result.exit_code != 0


def test_cli_doctor_json(tmp_path: Path) -> None:
    db = _fresh_ledger(tmp_path)
    result = runner.invoke(
        app,
        [
            "doctor",
            "--db",
            str(db),
            "--claude-dir",
            str(tmp_path / "nonexistent-claude"),
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] in {"CLEAN", "WARN", "FAIL"}
    assert any(f["check"] == "install" for f in payload["findings"])


# --- per-attempt model coverage (Story 28.1-002) ----------------------------


def _model_ledger(
    tmp_path: Path,
    *,
    model: str | None,
    status: str = "DONE",
    log: str | None = None,
) -> Path:
    """A ledger with one finished stage attempt and a chosen model attribution."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-002", "epic-28", "t", "Must", 3, "python-backend-engineer",
        "feature/28.1-002", None, "DONE",
    )
    ledger.stage_start(run_id, "28.1-002", "build", 1, model=model)
    ledger.stage_finish(run_id, "28.1-002", "build", 1, status)
    if log is not None:
        logs_dir = Path(f"{db}.logs") / run_id
        logs_dir.mkdir(parents=True)
        (logs_dir / "28.1-002-build-1.log").write_text(log, encoding="utf-8")
    return db


_MODEL_RESULT_LINE = json.dumps(
    {
        "type": "result",
        "result": "ok",
        "session_id": "s",
        "modelUsage": {"claude-opus-4-8": {"costUSD": 1.5, "outputTokens": 200}},
    }
) + "\n"


def test_model_coverage_clean_when_every_dispatched_row_is_attributed(
    tmp_path: Path,
) -> None:
    finding = check_model_coverage(_model_ledger(tmp_path, model="claude-opus-4-8"))
    assert finding.check == "model" and finding.status == "CLEAN"
    assert "1/1" in finding.detail and "100%" in finding.detail


def test_model_coverage_fails_on_a_fresh_run_null(tmp_path: Path) -> None:
    """AC3: a DONE stage whose own log names a model it failed to record.

    The transcript is what makes this a *regression* rather than an unknowable
    gap: the session emitted a `modelUsage` map, so the live recording had the
    model in hand and dropped it.
    """
    finding = check_model_coverage(
        _model_ledger(tmp_path, model=None, log=_MODEL_RESULT_LINE)
    )
    assert finding.status == "FAIL"
    assert "28.1-002/build#1" in finding.detail
    assert "regress" in finding.detail.lower()
    assert "model-backfill" in finding.remedy


def test_model_coverage_does_not_fail_a_reconcile_synthesized_done_row(
    tmp_path: Path,
) -> None:
    """A DONE row that never dispatched an agent is not a recording regression.

    `reconcile_run` — which runs on *every* close-out, not just the standalone
    verb — synthesizes DONE `build`/`coverage`/`review`/`merge` rows for a
    parked-then-landed story (`_ensure_stages_done`). No agent ran, so there is
    no transcript and no model to record. FAILing on those asserts a regression
    that did not happen and prints a remedy `model-backfill` cannot apply — and
    `sdlc doctor --exit-code` would then exit 2 forever.
    """
    finding = check_model_coverage(_model_ledger(tmp_path, model=None))
    assert finding.status == "CLEAN"
    assert "unrecoverable=1" in finding.detail
    assert "regress" not in finding.detail.lower()


def test_model_coverage_warns_when_the_null_is_only_recoverable_history(
    tmp_path: Path,
) -> None:
    """A NULL on a FAILED attempt is a gap to backfill, not a fresh-run defect."""
    finding = check_model_coverage(
        _model_ledger(tmp_path, model=None, status="FAILED", log=_MODEL_RESULT_LINE)
    )
    assert finding.status == "WARN"
    assert "recoverable=1" in finding.detail
    assert "model-backfill" in finding.remedy


def test_model_coverage_reports_unrecoverable_rows_without_coercing_them(
    tmp_path: Path,
) -> None:
    finding = check_model_coverage(
        _model_ledger(tmp_path, model=None, status="FAILED", log="plain text\n")
    )
    assert finding.status == "CLEAN"
    assert "unrecoverable=1" in finding.detail
    assert "0/1" in finding.detail


def test_model_coverage_stays_clean_when_the_remedy_would_change_nothing(
    tmp_path: Path,
) -> None:
    """An unrecoverable-only residual is not a WARN: no remedy can clear it.

    A plain-text `SDLC_AGENT_CMD` transcript names no model anywhere, so
    `model-backfill` updates zero rows against it. WARNing would print a remedy
    that provably does nothing and pin `sdlc doctor --exit-code` to 1 forever —
    the same argument the FAIL branch already makes one severity up. The rows
    stay *counted* in the detail (AC2: reported, never coerced), which is what
    the coverage number is for.
    """
    db = _model_ledger(tmp_path, model=None, status="FAILED", log="plain text\n")

    applied = backfill_models(Ledger(db), all_runs=True, apply=True)

    assert applied.updated == []  # the remedy is a provable no-op here
    assert check_model_coverage(db).status == "CLEAN"


def test_model_coverage_warns_on_the_recoverable_share_of_a_mixed_residual(
    tmp_path: Path,
) -> None:
    """One backfillable row is enough to warn, even beside unrecoverable ones."""
    db = _model_ledger(tmp_path, model=None, status="FAILED", log="plain text\n")
    ledger = Ledger(db)
    run_id = ledger.list_runs(limit=1)[0]["id"]
    ledger.stage_start(run_id, "28.1-002", "review", 1, model=None)
    ledger.stage_finish(run_id, "28.1-002", "review", 1, "FAILED")
    (Path(f"{db}.logs") / run_id / "28.1-002-review-1.log").write_text(
        _MODEL_RESULT_LINE, encoding="utf-8"
    )

    finding = check_model_coverage(db)

    assert finding.status == "WARN"
    assert "recoverable=1" in finding.detail and "unrecoverable=1" in finding.detail
    assert "model-backfill" in finding.remedy


def test_model_coverage_ignores_rows_that_never_dispatched(tmp_path: Path) -> None:
    """A SKIPPED (docs-only / cost-gated) row has a NULL model by design."""
    finding = check_model_coverage(_model_ledger(tmp_path, model=None, status="SKIPPED"))
    assert finding.status == "CLEAN"
    assert "no dispatched stage attempts" in finding.detail


def test_model_coverage_caps_the_listed_residual_rows(tmp_path: Path) -> None:
    """Doctor lists a few offenders then says how many it elided.

    The detail line has to stay readable when history is thin across many
    attempts, without under-reporting the size of the gap.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-002", "epic-28", "t", "Must", 3, "python-backend-engineer",
        "feature/28.1-002", None, "DONE",
    )
    # FAILED, so these are history to backfill rather than a fresh-run regression.
    for attempt in range(1, 8):
        ledger.stage_start(run_id, "28.1-002", "build", attempt, model=None)
        ledger.stage_finish(run_id, "28.1-002", "build", attempt, "FAILED")

    finding = check_model_coverage(db)

    assert finding.status == "CLEAN"
    assert "0/7" in finding.detail
    assert "unrecoverable=7" in finding.detail
    assert "+2 more" in finding.detail


def test_model_coverage_clean_without_a_ledger(tmp_path: Path) -> None:
    finding = check_model_coverage(tmp_path / "absent.db")
    assert finding.status == "CLEAN"


def test_model_coverage_is_read_only(tmp_path: Path) -> None:
    """Doctor reports; `sdlc model-backfill` is the verb that writes."""
    db = _model_ledger(tmp_path, model=None, status="FAILED", log=_MODEL_RESULT_LINE)
    check_model_coverage(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT model FROM stages").fetchone()[0] is None
    finally:
        conn.close()


def test_run_doctor_includes_the_model_coverage_finding(tmp_path: Path) -> None:
    report = _doctor(tmp_path, db_path=_model_ledger(tmp_path, model="claude-opus-4-8"))
    assert any(f.check == "model" for f in report.findings)


# ---------------------------------------------------------------------------
# Harness pin (#551 follow-up)
#
# A repo with no `.sdlc-harness.yaml` does not have "no routing" — it has
# *implicit* routing that follows the installed controller's registry `default:`.
# That registry ships inside the wheel, so `uv tool install --force` resets it:
# the same class of silent drift #543 and #551 fixed inside a run, one level up.
# ---------------------------------------------------------------------------


def _pin(root: Path, body: str) -> Path:
    path = root / ".sdlc-harness.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_harness_pin_clean_when_the_repo_pins_itself(tmp_path: Path) -> None:
    _pin(tmp_path, "harness:\n  default: codex\n")
    finding = check_harness_pin(tmp_path)
    assert finding.status == "CLEAN"
    assert "codex" in finding.detail
    assert finding.remedy == ""


def test_harness_pin_clean_names_a_per_role_map(tmp_path: Path) -> None:
    """A roles-only file is a pin too — it just doesn't cover every role."""
    _pin(tmp_path, "harness:\n  roles:\n    review: codex\n")
    finding = check_harness_pin(tmp_path)
    assert finding.status == "CLEAN"
    assert "review=codex" in finding.detail


def test_harness_pin_warns_when_unpinned(tmp_path: Path) -> None:
    """The common case: no file, so routing follows whatever is installed."""
    finding = check_harness_pin(tmp_path)
    assert finding.status == "WARN"
    assert ".sdlc-harness.yaml" in finding.detail
    assert "sdlc-harness.yaml" in finding.remedy


def test_harness_pin_warns_harder_when_the_registry_default_is_not_claude(
    tmp_path: Path,
) -> None:
    """Unpinned *and* following a non-default harness is the dangerous variant.

    A reinstall resets the registry to `claude`, so this repo would silently move
    off codex — with nothing in the repo recording that it ever ran on codex.
    """
    finding = check_harness_pin(tmp_path, registry_default="codex")
    assert finding.status == "WARN"
    assert "codex" in finding.detail
    assert "reinstall" in finding.detail.lower() or "reset" in finding.detail.lower()


def test_harness_pin_fails_on_a_malformed_file(tmp_path: Path) -> None:
    """A broken pin is not a missing pin — it exits 2 on every run until fixed."""
    _pin(tmp_path, "harness:\n  default: []\n")
    finding = check_harness_pin(tmp_path)
    assert finding.status == "FAIL"
    assert "default" in finding.detail


def test_harness_pin_fails_on_an_unknown_harness_name(tmp_path: Path) -> None:
    """Parses fine, but the registry preflight would reject it mid-adoption."""
    _pin(tmp_path, "harness:\n  default: gpt9\n")
    finding = check_harness_pin(tmp_path)
    assert finding.status == "FAIL"
    assert "gpt9" in finding.detail


def test_run_doctor_includes_the_harness_pin_finding(tmp_path: Path) -> None:
    report = run_doctor(
        repo_root=tmp_path,
        claude_dir=tmp_path / "claude",
        db_path=tmp_path / ".sdlc-state.db",
        registry=Registry(tmp_path / "registry.json"),
        dep_probe=lambda _tool: True,
    )
    assert any(f.check == "harness" for f in report.findings)


def test_harness_pin_reads_the_real_registry_default_when_not_injected(
    tmp_path: Path,
) -> None:
    """Uninjected, the check resolves the shipped registry's own `default:`."""
    finding = check_harness_pin(tmp_path)
    assert finding.status == "WARN"
    # The bundled registry ships `default: claude`.
    assert "claude" in finding.detail


def test_harness_pin_degrades_when_the_registry_cannot_be_read(
    tmp_path: Path, monkeypatch
) -> None:
    """Doctor is a diagnostic: an unreadable registry must not make it raise.

    An unreadable registry is already the config check's business, and an
    unverifiable harness name must not be reported as undefined — doctor should
    never invent a failure the operator cannot act on.
    """
    import sdlc.role_routing as rr

    def boom(*args, **kwargs):
        raise OSError("registry unreadable")

    monkeypatch.setattr(rr, "registry_default_harness", boom)
    monkeypatch.setattr(rr, "default_registry_path", boom)

    # Unpinned: falls back to the built-in default rather than raising.
    assert check_harness_pin(tmp_path).status == "WARN"

    # Pinned to something unverifiable: trusted, not failed.
    _pin(tmp_path, "harness:\n  default: something-exotic\n")
    assert check_harness_pin(tmp_path).status == "CLEAN"


# ---------------------------------------------------------------------------
# Installed controller vs checkout (Story 15.1-004)
#
# Field finding: the PATH-installed `sdlc` is a `uv tool install` snapshot, not
# the checkout. Merging to `main` never updates it, and nothing said so — this
# check names both versions before any tokens are spent.
# ---------------------------------------------------------------------------


def _declare_checkout_version(root: Path, version: str) -> Path:
    controller_dir = root / "controller"
    controller_dir.mkdir(parents=True, exist_ok=True)
    path = controller_dir / "pyproject.toml"
    path.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    return path


def test_controller_version_clean_when_equal(tmp_path: Path) -> None:
    _declare_checkout_version(tmp_path, "2.57.0")
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "CLEAN"
    assert finding.remedy == ""


def test_controller_version_warns_when_installed_is_behind(tmp_path: Path) -> None:
    _declare_checkout_version(tmp_path, "2.57.0")
    finding = check_controller_version(tmp_path, installed_version="2.45.12")
    assert finding.status == "WARN"
    assert "installed 2.45.12" in finding.detail
    assert "checkout 2.57.0" in finding.detail
    assert "install-controller.sh" in finding.remedy
    assert "sdlc dashboard --restart" in finding.remedy


def test_controller_version_warns_when_installed_is_ahead(tmp_path: Path) -> None:
    """The two disagree either way — but the remedy never suggests going backwards."""
    _declare_checkout_version(tmp_path, "2.45.12")
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "WARN"
    assert "installed 2.57.0" in finding.detail
    assert "checkout 2.45.12" in finding.detail
    assert "install-controller.sh" not in finding.remedy
    assert "git pull" in finding.remedy


def test_controller_version_not_applicable_when_no_controller_pyproject(
    tmp_path: Path,
) -> None:
    """Any non-framework repo `sdlc` points at declares no controller version."""
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "CLEAN"
    assert finding.remedy == ""


def test_controller_version_clean_in_dev_mode(tmp_path: Path) -> None:
    """`uv run` reads the same checkout `_resolve_version()` does — never a false WARN."""
    _declare_checkout_version(tmp_path, "2.57.0")
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "CLEAN"


def test_run_doctor_includes_the_controller_version_finding(tmp_path: Path) -> None:
    report = _doctor(tmp_path)
    assert any(f.name == "Installed controller vs checkout" for f in report.findings)


def test_controller_version_clean_when_numerically_equal_but_written_differently(
    tmp_path: Path,
) -> None:
    """`2.57` and `2.57.0` are the same release — no noise over formatting."""
    _declare_checkout_version(tmp_path, "2.57.0")
    finding = check_controller_version(tmp_path, installed_version="2.57")
    assert finding.status == "CLEAN"


def test_controller_version_not_applicable_on_malformed_pyproject(
    tmp_path: Path,
) -> None:
    """A checkout `pyproject.toml` with no readable version must not crash doctor."""
    controller_dir = tmp_path / "controller"
    controller_dir.mkdir()
    (controller_dir / "pyproject.toml").write_text("not valid toml [[[", encoding="utf-8")
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "CLEAN"
    assert finding.remedy == ""


def test_controller_version_not_applicable_when_project_table_has_no_version(
    tmp_path: Path,
) -> None:
    controller_dir = tmp_path / "controller"
    controller_dir.mkdir()
    (controller_dir / "pyproject.toml").write_text(
        "[project]\nname = \"sdlc-controller\"\n", encoding="utf-8"
    )
    finding = check_controller_version(tmp_path, installed_version="2.57.0")
    assert finding.status == "CLEAN"


def test_compare_versions_falls_back_to_tuple_parse_when_packaging_is_unavailable(
    monkeypatch,
) -> None:
    """`packaging` is a transitive dependency — verify the stdlib fallback too."""
    import sys

    from sdlc.doctor import _compare_versions

    monkeypatch.setitem(sys.modules, "packaging.version", None)
    assert _compare_versions("2.45.12", "2.57.0") < 0
    assert _compare_versions("2.57.0", "2.45.12") > 0
    assert _compare_versions("2.57.0", "2.57.0") == 0


def test_parse_semver_tolerates_a_prerelease_suffix() -> None:
    from sdlc.doctor import _parse_semver

    assert _parse_semver("2.57.0") == (2, 57, 0)
    assert _parse_semver("2.57.0rc1") == (2, 57, 0)
    assert _parse_semver("2.57") == (2, 57, 0)


# --- Issue #709: the version guard reads the base ref, not the working tree ---


def _git(root: Path, *args: str) -> str:
    """Run git with an explicit identity — CI job containers carry no gitconfig."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _controller_repo(root: Path, main_version: str) -> Path:
    """A git repo whose ``main`` declares ``main_version`` and ships an installer."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _declare_checkout_version(root, main_version)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "install-controller.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"release {main_version}")
    return root


def test_controller_version_reads_main_not_a_parked_feature_branch(tmp_path: Path) -> None:
    """A parked merge leaves the checkout on an older branch — never 'installed ahead'."""
    repo = _controller_repo(tmp_path / "repo", "2.71.2")
    _git(repo, "checkout", "-q", "-b", "feature/issue-693")
    _declare_checkout_version(repo, "2.71.2")
    _git(repo, "checkout", "-q", "main")
    _declare_checkout_version(repo, "2.71.4")
    _git(repo, "commit", "-qam", "release 2.71.4")
    _git(repo, "checkout", "-q", "feature/issue-693")

    finding = check_controller_version(repo, installed_version="2.71.4")

    assert finding.status == "CLEAN", finding.detail
    assert "2.71.4" in finding.detail


def test_controller_version_prefers_origin_main_over_local_main(tmp_path: Path) -> None:
    upstream = _controller_repo(tmp_path / "upstream", "2.71.4")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    _declare_checkout_version(upstream, "2.71.5")
    _git(upstream, "commit", "-qam", "release 2.71.5")
    _git(clone, "fetch", "-q", "origin")

    finding = check_controller_version(clone, installed_version="2.71.4")

    assert finding.status == "WARN"
    assert "checkout 2.71.5" in finding.detail
    assert "install-controller.sh" in finding.remedy


def test_base_ref_controller_version_is_none_outside_git(tmp_path: Path) -> None:
    from sdlc.doctor import base_ref_controller_version

    _declare_checkout_version(tmp_path, "2.71.4")
    assert base_ref_controller_version(tmp_path) is None


# --- Issue #709: self-update from a clean base-ref worktree -------------------


class _Installer:
    """Records the tree it was asked to install from, and what it declared."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.trees: list[Path] = []
        self.versions: list[str] = []

    def __call__(self, tree: Path) -> bool:
        self.trees.append(tree)
        text = (tree / "controller" / "pyproject.toml").read_text(encoding="utf-8")
        self.versions.append(tomllib.loads(text)["project"]["version"])
        assert (tree / "scripts" / "install-controller.sh").is_file()
        return self.ok


def test_self_update_installs_from_main_never_the_checked_out_branch(tmp_path: Path) -> None:
    from sdlc.doctor import self_update_controller

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    _git(repo, "checkout", "-q", "-b", "feature/old")
    _declare_checkout_version(repo, "2.71.2")
    _git(repo, "commit", "-qam", "old branch")
    installer = _Installer()

    new = self_update_controller(repo, "2.71.3", installer=installer, fetch=False)

    assert new == "2.71.4"
    assert installer.versions == ["2.71.4"]
    # The throwaway worktree is gone, and the operator's checkout is untouched.
    assert not installer.trees[0].exists()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature/old"
    assert "2.71.4" not in _git(repo, "worktree", "list")


@pytest.mark.parametrize("installed", ["2.71.4", "2.71.9"])
def test_self_update_never_installs_when_main_is_not_ahead(
    tmp_path: Path, installed: str,
) -> None:
    from sdlc.doctor import self_update_controller

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    installer = _Installer()

    assert self_update_controller(repo, installed, installer=installer, fetch=False) is None
    assert installer.trees == []


def test_self_update_reports_none_when_the_installer_fails(tmp_path: Path) -> None:
    from sdlc.doctor import self_update_controller

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    installer = _Installer(ok=False)

    assert self_update_controller(repo, "2.71.3", installer=installer, fetch=False) is None
    assert installer.trees and not installer.trees[0].exists()


def test_self_update_is_a_no_op_outside_a_controller_repo(tmp_path: Path) -> None:
    from sdlc.doctor import self_update_controller

    installer = _Installer()
    # fetch=True too: a repo with no controller pyproject never touches the network.
    assert self_update_controller(tmp_path, "2.71.3", installer=installer) is None
    assert installer.trees == []


# --- Issue #709: coverage of the self-update edges ----------------------------


def test_self_update_fetches_origin_main_before_deciding(tmp_path: Path) -> None:
    from sdlc.doctor import self_update_controller

    upstream = _controller_repo(tmp_path / "upstream", "2.71.4")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    _declare_checkout_version(upstream, "2.71.5")
    _git(upstream, "commit", "-qam", "release 2.71.5")
    installer = _Installer()

    new = self_update_controller(clone, "2.71.4", installer=installer)

    assert new == "2.71.5"
    assert installer.versions == ["2.71.5"]


def test_self_update_survives_a_failing_fetch(tmp_path: Path) -> None:
    from sdlc.doctor import self_update_controller

    repo = _controller_repo(tmp_path / "repo", "2.71.4")  # no `origin` remote
    installer = _Installer()

    assert self_update_controller(repo, "2.71.3", installer=installer) == "2.71.4"


def test_self_update_swallows_a_git_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc import doctor

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    real_run = subprocess.run

    def flaky(cmd, *a, **kw):  # noqa: ANN001
        if "worktree" in cmd and "add" in cmd:
            raise OSError("boom")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(doctor.subprocess, "run", flaky)
    installer = _Installer()

    assert doctor.self_update_controller(repo, "2.71.3", installer=installer, fetch=False) is None
    assert installer.trees == []


def test_base_ref_lookup_tolerates_a_missing_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc import doctor

    def gone(*a, **kw):  # noqa: ANN002, ANN003
        raise OSError("no git")

    monkeypatch.setattr(doctor.subprocess, "run", gone)

    assert doctor.base_ref_controller_version(tmp_path) is None
    assert doctor.self_update_controller(tmp_path, "1.0.0", fetch=False) is None


def test_base_ref_version_is_none_for_an_unreadable_pyproject(tmp_path: Path) -> None:
    from sdlc.doctor import base_ref_controller_version

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    (repo / "controller" / "pyproject.toml").write_text("not = [valid", encoding="utf-8")
    _git(repo, "commit", "-qam", "break pyproject")

    assert base_ref_controller_version(repo) is None


def test_controller_version_falls_back_to_the_working_tree_outside_git(tmp_path: Path) -> None:
    _declare_checkout_version(tmp_path, "2.71.4")
    assert check_controller_version(tmp_path, installed_version="2.71.4").status == "CLEAN"

    (tmp_path / "controller" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    finding = check_controller_version(tmp_path, installed_version="2.71.4")
    assert finding.status == "CLEAN"
    assert "no readable" in finding.detail


def test_run_install_script_reports_exit_status(tmp_path: Path) -> None:
    from sdlc.doctor import _run_install_script

    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "install-controller.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert _run_install_script(tmp_path) is True
    script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    assert _run_install_script(tmp_path) is False


def test_run_install_script_is_false_when_bash_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc import doctor

    def gone(*a, **kw):  # noqa: ANN002, ANN003
        raise OSError("no bash")

    monkeypatch.setattr(doctor.subprocess, "run", gone)
    assert doctor._run_install_script(tmp_path) is False


@pytest.mark.parametrize("error", [OSError("git vanished"),
                                   subprocess.TimeoutExpired("git", 60)])
def test_self_update_cleanup_failure_still_reports_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    """A `worktree remove` that raises must not escape — the install stood."""
    from sdlc import doctor

    repo = _controller_repo(tmp_path / "repo", "2.71.4")
    real_run = subprocess.run

    def flaky(cmd, *a, **kw):
        if "worktree" in cmd and "remove" in cmd:
            raise error
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(doctor.subprocess, "run", flaky)
    installer = _Installer()

    assert doctor.self_update_controller(repo, "2.71.3", installer=installer, fetch=False) == "2.71.4"
    assert not installer.trees[0].exists()


# --- queue service (Story 35.1-003) -----------------------------------------


def _service_plist(
    tmp_path: Path,
    *,
    bind: str = "100.101.102.103:8790",
    store: str | None = None,
    err_log: str | None = None,
) -> Path:
    env = {"SDLC_QUEUE_PATH": store} if store else {}
    body: dict[str, object] = {
        "Label": "com.fxmartin.sdlc-queue",
        "ProgramArguments": ["/usr/local/bin/sdlc", "queue", "serve", "--bind", bind],
        "EnvironmentVariables": env,
    }
    if err_log:
        body["StandardErrorPath"] = err_log
    path = tmp_path / "com.fxmartin.sdlc-queue.plist"
    path.write_bytes(plistlib.dumps(body))
    return path


def test_queue_service_not_installed_is_not_applicable(tmp_path: Path) -> None:
    finding = check_queue_service(tmp_path / "absent.plist", probe=lambda h, p: False)
    assert (finding.check, finding.status) == ("queue-service", "CLEAN")
    assert "not installed" in finding.detail


def test_queue_service_running_reports_bind_and_store(tmp_path: Path) -> None:
    store = str(tmp_path / "queue.db")  # the shell's own, via _isolated_queue
    seen: list[tuple[str, int]] = []

    def probe(host: str, port: int) -> bool:
        seen.append((host, port))
        return True

    finding = check_queue_service(_service_plist(tmp_path, store=store), probe=probe)
    assert finding.status == "CLEAN"
    assert seen == [("100.101.102.103", 8790)]
    assert "running" in finding.detail
    assert "100.101.102.103:8790" in finding.detail
    assert store in finding.detail


def test_queue_service_down_fails_with_a_remedy(tmp_path: Path) -> None:
    finding = check_queue_service(_service_plist(tmp_path), probe=lambda h, p: False)
    assert finding.status == "FAIL"
    assert "100.101.102.103:8790" in finding.detail
    assert "launchctl kickstart" in finding.remedy


def test_queue_service_down_points_at_the_plists_own_error_log(tmp_path: Path) -> None:
    # A custom or nix-rendered install may log elsewhere; the plist says where.
    plist = _service_plist(tmp_path, err_log="/srv/logs/sdlc-queue.err")
    finding = check_queue_service(plist, probe=lambda h, p: False)
    assert "/srv/logs/sdlc-queue.err" in finding.remedy
    assert ".local/state/sdlc" not in finding.remedy


def test_queue_service_down_without_an_error_log_names_none(tmp_path: Path) -> None:
    # No StandardErrorPath: launchd discards stderr, so there is no file to read.
    finding = check_queue_service(_service_plist(tmp_path), probe=lambda h, p: False)
    assert "launchctl kickstart" in finding.remedy
    assert "err.log" not in finding.remedy


def test_queue_service_store_follows_launchds_bare_environment(tmp_path: Path) -> None:
    # No SDLC_QUEUE_PATH/XDG_STATE_HOME in the plist: the service resolves the
    # registry-sibling default under HOME, not whatever this shell exports.
    finding = check_queue_service(_service_plist(tmp_path), probe=lambda h, p: True)
    assert str(Path.home() / ".sdlc" / "queue.db") in finding.detail


def test_queue_service_store_honours_the_plists_xdg_state_home(tmp_path: Path) -> None:
    path = tmp_path / "xdg.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "ProgramArguments": ["sdlc", "queue", "serve", "--bind=100.64.0.9:8790"],
                "EnvironmentVariables": {"XDG_STATE_HOME": "/srv/state"},
            }
        )
    )
    finding = check_queue_service(path, probe=lambda h, p: True)
    assert "/srv/state/sdlc/queue.db" in finding.detail
    assert "100.64.0.9:8790" in finding.detail


def test_queue_service_store_differing_from_the_shells_warns(tmp_path: Path) -> None:
    other = str(tmp_path / "elsewhere" / "queue.db")
    finding = check_queue_service(
        _service_plist(tmp_path, store=other), probe=lambda h, p: True
    )
    assert finding.status == "WARN"
    assert other in finding.detail
    assert "SDLC_QUEUE_PATH" in finding.remedy


def test_queue_service_plist_without_a_bind_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.plist"
    path.write_bytes(plistlib.dumps({"ProgramArguments": ["/usr/local/bin/sdlc"]}))
    finding = check_queue_service(path, probe=lambda h, p: True)
    assert finding.status == "FAIL"
    assert "--bind" in finding.detail


def test_queue_service_unreadable_plist_fails(tmp_path: Path) -> None:
    path = tmp_path / "junk.plist"
    path.write_text("not a plist", encoding="utf-8")
    assert check_queue_service(path, probe=lambda h, p: True).status == "FAIL"


@pytest.mark.parametrize(
    "body",
    [
        # Cut short, e.g. an interrupted `sed ... > plist` install.
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
            "<key>Label</key>\n<string>com.fxmartin.sdlc-queue</string>\n"
        ),
        # Well-formed shape, but an unescaped `&` in a hand-edited value.
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            "<key>A</key><string>a & b</string></dict></plist>\n"
        ),
        # Valid XML and a valid plist, but ProgramArguments is not an array.
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            "<key>ProgramArguments</key><integer>5</integer></dict></plist>\n"
        ),
        # Valid XML that plistlib's parser trips over (a key outside any dict).
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">'
            "<key>Label</key></plist>\n"
        ),
    ],
    ids=["truncated", "unescaped-ampersand", "scalar-program-arguments", "stray-key"],
)
def test_queue_service_malformed_plist_fails(tmp_path: Path, body: str) -> None:
    # Whatever plistlib or the shape coercion raises (ExpatError, TypeError,
    # IndexError, ...) must read as FAIL, not crash `sdlc doctor` (and `sdlc
    # status --markdown`).
    path = tmp_path / "half-written.plist"
    path.write_text(body, encoding="utf-8")
    finding = check_queue_service(path, probe=lambda h, p: True)
    assert finding.status == "FAIL"
    assert "unreadable" in finding.detail


def test_queue_service_default_probe_sees_a_real_listener(tmp_path: Path) -> None:
    store = str(tmp_path / "queue.db")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        plist = _service_plist(tmp_path, bind=f"127.0.0.1:{port}", store=store)
        up = check_queue_service(plist)
    assert up.status == "CLEAN"
    # The listener is closed now, so the same bind is refused.
    assert check_queue_service(plist).status == "FAIL"


def test_run_doctor_includes_the_queue_service_finding(tmp_path: Path) -> None:
    # run_doctor uses the real probe, so point it at a loopback port just freed:
    # refused at once, and never a packet off-host (a tailnet/CGNAT address would
    # route out the default gateway and burn the whole probe timeout).
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    plist = _service_plist(
        tmp_path, bind=f"127.0.0.1:{port}", store=str(tmp_path / "queue.db")
    )
    report = _doctor(tmp_path, queue_service_plist=plist)
    assert _finding(report, "queue-service").status == "FAIL"


def test_run_doctor_queue_service_defaults_to_the_launchagents_plist(
    tmp_path: Path,
) -> None:
    # HOME is a fresh tmp dir (autouse fixture): no plist, so not applicable.
    assert _finding(_doctor(tmp_path), "queue-service").status == "CLEAN"


def _plist_with_args(tmp_path: Path, args: list[str]) -> Path:
    path = tmp_path / "com.fxmartin.sdlc-queue.plist"
    path.write_bytes(plistlib.dumps({"ProgramArguments": args}))
    return path


def test_queue_service_non_numeric_port_reads_as_down(tmp_path: Path) -> None:
    plist = _plist_with_args(tmp_path, ["sdlc", "queue", "serve", "--bind", "host:notaport"])
    finding = check_queue_service(plist, probe=lambda h, p: True)
    assert finding.status == "FAIL"
    assert "nothing is listening on host:notaport" in finding.detail


def test_queue_service_accepts_the_equals_bind_form(tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []
    plist = _plist_with_args(tmp_path, ["sdlc", "queue", "serve", "--bind=10.0.0.1:9000"])
    finding = check_queue_service(
        plist,
        probe=lambda h, p: seen.append((h, p)) or True,
        queue_path=Path.home() / ".sdlc" / "queue.db",
    )
    assert seen == [("10.0.0.1", 9000)]
    assert finding.status == "CLEAN"


def test_queue_service_strips_ipv6_brackets_for_the_probe(tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []
    plist = _plist_with_args(tmp_path, ["sdlc", "queue", "serve", "--bind", "[::1]:9000"])
    check_queue_service(plist, probe=lambda h, p: seen.append((h, p)) or True)
    assert seen == [("::1", 9000)]


def test_queue_service_dangling_bind_flag_fails(tmp_path: Path) -> None:
    plist = _plist_with_args(tmp_path, ["sdlc", "queue", "serve", "--bind"])
    finding = check_queue_service(plist, probe=lambda h, p: True)
    assert finding.status == "FAIL"
    assert "--bind" in finding.detail


def test_queue_service_store_falls_back_to_xdg_then_home() -> None:
    from sdlc.doctor import _service_store_path

    assert _service_store_path({"XDG_STATE_HOME": "/x"}) == Path("/x/sdlc/queue.db")
    assert _service_store_path({}) == Path.home() / ".sdlc" / "queue.db"


# --- Story 35.2-004: the resident worker is registered and online -----------------------


def _worker_record(host: str, *, seconds_ago: float):
    from sdlc.queue import WorkerRecord

    beat = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    return WorkerRecord(name="m3max", host=host, registered_at=beat, last_heartbeat=beat)


def _worker_plist(tmp_path: Path, *, store: Path) -> Path:
    """An installed worker LaunchAgent that pins ``store``, as the template does."""
    path = tmp_path / "com.fxmartin.sdlc-worker.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.fxmartin.sdlc-worker",
                "ProgramArguments": ["/Users/fx/.local/bin/sdlc", "queue", "run", "--worker", "m3max"],
                "EnvironmentVariables": {"SDLC_QUEUE_PATH": str(store)},
            }
        )
    )
    return path


def _heartbeat(store: Path, *, host: str = "m3") -> None:
    """What the resident worker does every 30 s: register itself in ``store``."""
    from sdlc.queue import QueueStore

    queue = QueueStore(store)
    queue.init()
    queue.register_worker("m3max", host=host)


def test_fleet_worker_online_is_clean() -> None:
    from sdlc.doctor import check_fleet_worker

    finding = check_fleet_worker(lambda: [_worker_record("m3", seconds_ago=5)], host="m3")

    assert finding.status == "CLEAN"
    assert "m3max" in finding.detail and "online" in finding.detail


def test_fleet_worker_that_stopped_heartbeating_fails() -> None:
    from sdlc.doctor import check_fleet_worker

    finding = check_fleet_worker(lambda: [_worker_record("m3", seconds_ago=600)], host="m3")

    assert finding.status == "FAIL"
    assert "offline" in finding.detail
    assert "worker.log" in finding.remedy


def test_fleet_worker_never_registered_fails() -> None:
    from sdlc.doctor import check_fleet_worker

    # A worker on another host does not count for this one.
    finding = check_fleet_worker(lambda: [_worker_record("xps", seconds_ago=5)], host="m3")

    assert finding.status == "FAIL"
    assert "not registered" in finding.detail


def test_fleet_worker_unreachable_queue_fails() -> None:
    from sdlc.doctor import check_fleet_worker
    from sdlc.queue import QueueError

    def boom():
        raise QueueError("fleet queue unreachable")

    finding = check_fleet_worker(boom, host="m3")

    assert finding.status == "FAIL"
    assert "unreachable" in finding.detail


def test_fleet_worker_check_is_skipped_without_the_launch_agent(tmp_path) -> None:
    from sdlc.doctor import check_fleet_worker_installed

    assert check_fleet_worker_installed(agent_path=tmp_path / "absent.plist") is None


def test_fleet_worker_check_runs_when_the_launch_agent_is_installed(tmp_path) -> None:
    from sdlc.doctor import check_fleet_worker_installed

    agent = _worker_plist(tmp_path, store=tmp_path / "queue.db")

    finding = check_fleet_worker_installed(agent_path=agent, host="m3")

    assert finding is not None
    assert finding.status == "FAIL"  # installed, but nothing has registered in an empty queue
    assert "not registered" in finding.detail


def test_fleet_worker_sharing_the_shells_store_is_clean(tmp_path) -> None:
    from sdlc.doctor import check_fleet_worker_installed

    store = tmp_path / "queue.db"
    _heartbeat(store)

    finding = check_fleet_worker_installed(
        agent_path=_worker_plist(tmp_path, store=store), host="m3", queue_path=store
    )

    assert finding is not None
    assert finding.status == "CLEAN"
    assert "m3max" in finding.detail and "online" in finding.detail


def test_fleet_worker_is_looked_up_in_the_store_its_plist_pins(tmp_path) -> None:
    # launchd starts the worker with the plist's environment, not this shell's,
    # and `queue run` is local-only: a worker heartbeating in its pinned store is
    # online, however this shell resolves its own queue. But jobs enqueued here
    # land in a file it never drains, so the shell must be told.
    from sdlc.doctor import check_fleet_worker_installed

    pinned = tmp_path / "state" / "sdlc" / "queue.db"
    pinned.parent.mkdir(parents=True)
    _heartbeat(pinned)
    shells = tmp_path / "home" / ".sdlc" / "queue.db"

    finding = check_fleet_worker_installed(
        agent_path=_worker_plist(tmp_path, store=pinned), host="m3", queue_path=shells
    )

    assert finding is not None
    assert finding.status == "WARN"
    assert "m3max" in finding.detail and "online" in finding.detail
    assert str(shells) in finding.detail
    assert f"SDLC_QUEUE_PATH={pinned}" in finding.remedy


def test_fleet_worker_warns_while_this_shell_enqueues_to_a_fleet_queue(tmp_path, monkeypatch) -> None:
    # The same split by another route: with SDLC_QUEUE_URL exported here,
    # `--enqueue` lands on the fleet service, which launchd's bare environment
    # never points the worker at — and `queue run` cannot drain it yet anyway.
    from sdlc.doctor import check_fleet_worker_installed

    store = tmp_path / "queue.db"
    _heartbeat(store)
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://home-lab:8790")

    finding = check_fleet_worker_installed(
        agent_path=_worker_plist(tmp_path, store=store), host="m3", queue_path=store
    )

    assert finding is not None
    assert finding.status == "WARN"
    assert "m3max" in finding.detail and "online" in finding.detail
    assert "http://home-lab:8790" in finding.detail
    assert "SDLC_QUEUE_URL" in finding.remedy


def test_fleet_worker_leaves_a_malformed_queue_url_to_the_fleet_queue_finding(
    tmp_path, monkeypatch
) -> None:
    # `--enqueue` fails loudly on it rather than going anywhere, and the
    # `fleet-queue` finding already FAILs it — this one must not crash on it.
    from sdlc.doctor import check_fleet_worker_installed

    store = tmp_path / "queue.db"
    _heartbeat(store)
    monkeypatch.setenv("SDLC_QUEUE_URL", "not a url")

    finding = check_fleet_worker_installed(
        agent_path=_worker_plist(tmp_path, store=store), host="m3", queue_path=store
    )

    assert finding is not None
    assert finding.status == "CLEAN"


def test_fleet_worker_on_a_corrupt_store_fails_instead_of_crashing(tmp_path) -> None:
    # `QueueStore.list_workers` absorbs only a missing table; a file that is not
    # a database raises sqlite3.DatabaseError, which must not escape doctor.
    from sdlc.doctor import check_fleet_worker_installed

    store = tmp_path / "queue.db"
    store.write_bytes(b"this is not a sqlite database " * 8)

    finding = check_fleet_worker_installed(
        agent_path=_worker_plist(tmp_path, store=store), host="m3", queue_path=store
    )

    assert finding is not None
    assert finding.status == "FAIL"
    assert "not a database" in finding.detail


@pytest.mark.parametrize("body", [b"not a plist", b"<plist/>"], ids=["junk", "no-dict"])
def test_fleet_worker_unreadable_plist_fails(tmp_path, body: bytes) -> None:
    from sdlc.doctor import check_fleet_worker_installed

    agent = tmp_path / "com.fxmartin.sdlc-worker.plist"
    agent.write_bytes(body)

    finding = check_fleet_worker_installed(agent_path=agent, host="m3")

    assert finding is not None
    assert finding.status == "FAIL"
    assert "unreadable" in finding.detail
    assert "templates/launchd/com.fxmartin.sdlc-worker.plist" in finding.remedy


def test_run_doctor_reports_the_worker_only_when_its_launch_agent_is_installed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    claude_dir, repo_root = _healthy_install(tmp_path)

    def doctor(worker_plist: Path) -> DoctorReport:
        return run_doctor(
            repo_root=repo_root,
            claude_dir=claude_dir,
            db_path=tmp_path / "ledger.db",
            queue_path=tmp_path / "queue.db",
            registry=Registry(tmp_path / "registry.json"),
            dep_probe=lambda _b: True,
            worker_plist=worker_plist,
        )

    assert not any(f.check == "fleet-worker" for f in doctor(tmp_path / "absent.plist").findings)

    agent = _worker_plist(tmp_path, store=tmp_path / "queue.db")
    # Installed, but nothing has registered in this empty queue.
    finding = _finding(doctor(agent), "fleet-worker")
    assert finding.status == "FAIL"
    assert "not registered" in finding.detail


def test_run_doctor_reports_a_corrupt_queue_with_the_worker_installed(tmp_path) -> None:
    # Doctor is safe to run anywhere: with the worker installed, the corrupt store
    # `check_queue` already reports must stay a finding, not become a traceback
    # out of `sdlc doctor` / `sdlc status --markdown`.
    claude_dir, repo_root = _healthy_install(tmp_path)
    store = tmp_path / "queue.db"
    store.write_bytes(b"this is not a sqlite database " * 8)

    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=tmp_path / "ledger.db",
        queue_path=store,
        registry=Registry(tmp_path / "registry.json"),
        dep_probe=lambda _b: True,
        worker_plist=_worker_plist(tmp_path, store=store),
    )

    assert _finding(report, "queue").status == "FAIL"
    assert _finding(report, "fleet-worker").status == "FAIL"


def test_doctor_never_reads_a_real_worker_launch_agent_under_test() -> None:
    # A developer Mac with the worker installed must not leak its live queue into
    # the suite: conftest points the default at a path that does not exist.
    from sdlc.doctor import default_worker_plist

    assert not default_worker_plist().exists()
    assert default_worker_plist().name == "com.fxmartin.sdlc-worker.plist"
