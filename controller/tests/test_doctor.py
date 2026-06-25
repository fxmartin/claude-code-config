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
