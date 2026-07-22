# ABOUTME: Tests for `sdlc doctor` — health-check across install/ledger/runs/config/deps.
# ABOUTME: Story 15.1-001. Seeds broken fixtures (missing dep, stale ledger, stuck run).

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app
from sdlc.doctor import (
    MANAGED_PATHS,
    DoctorReport,
    Finding,
    check_model_coverage,
    check_usage_agreement,
    run_doctor,
    worst_status,
)
from sdlc.model_backfill import backfill_models
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


def test_ledger_that_cannot_be_opened_fails(tmp_path: Path) -> None:
    """A path that exists but is not an openable database is a FAIL, not a crash."""
    db = tmp_path / "ledger-is-a-directory.db"
    db.mkdir()
    report = _doctor(tmp_path, db_path=db)
    ledger = _finding(report, "ledger")
    assert ledger.status == "FAIL"
    assert "could not be opened" in ledger.detail
    assert ledger.remedy


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
