# ABOUTME: Tests for `sdlc doctor` — health-check across install/ledger/runs/config/deps.
# ABOUTME: Story 15.1-001. Seeds broken fixtures (missing dep, stale ledger, stuck run).

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app
from sdlc.doctor import (
    MANAGED_PATHS,
    DoctorReport,
    Finding,
    check_usage_agreement,
    run_doctor,
    worst_status,
)
from sdlc.registry import Registry, RunRecord

runner = CliRunner()


# --- fixtures ---------------------------------------------------------------


def _healthy_install(tmp_path: Path) -> tuple[Path, Path]:
    """Build a healthy ~/.claude install symlinked into a repo root.

    Returns ``(claude_dir, repo_root)``.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # A valid settings.json so the config check passes.
    (repo_root / "settings.json").write_text("{}\n", encoding="utf-8")
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
    # A dependency finding per tool.
    assert sum(1 for f in report.findings if f.check == "dependency") == 4
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
