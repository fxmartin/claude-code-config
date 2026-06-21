# ABOUTME: Tests for the `sdlc reconcile` recovery verb CLI wiring (Story 12.3-002).
# ABOUTME: Drives reconcile via CliRunner over real git+origin fixtures and stale DBs.

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

from test_ledger_migrate import _columns, _old_schema_db, _USAGE_COLS
from test_reconcile import _checkout, _commit, _git, _seed_run, _status, _merge_done

runner = CliRunner()


# --- git fixture helpers ----------------------------------------------------


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """A work repo whose ``origin`` is a fetchable bare remote, on branch main.

    Reconcile fetches ``origin`` first, so a faithful CLI test needs a real
    (local) remote rather than the remote-less repos in test_reconcile.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "-C", str(origin), "init", "-q", "--bare"], check=True
    )

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "main")
    _git(root, "remote", "set-head", "origin", "main")
    return root


def _land_story(root: Path, story_id: str) -> None:
    """Create ``feature/<id>``, ff-merge to main, push so origin/main carries it."""
    _checkout(root, f"feature/{story_id}", new=True)
    _commit(root, f"{story_id}.py", "x = 1\n", f"feat: work (#{story_id})")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", f"feature/{story_id}")
    _git(root, "push", "-q", "origin", "main")


# --- 429-aborted-then-merged fixture reconciles FAILED → DONE ---------------


def test_reconcile_failed_to_done(tmp_path: Path, monkeypatch) -> None:
    """The headline recovery case: an aborted run whose PR merged by hand."""
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-001", "FAILED", 100)])

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "99.1-001" in result.output
    assert "FAILED" in result.output and "DONE" in result.output
    assert _status(db, run_id, "99.1-001") == "DONE"
    assert _merge_done(db, run_id, "99.1-001") == 1


def test_reconcile_idempotent_rerun(tmp_path: Path, monkeypatch) -> None:
    """A second reconcile flips nothing and reports "nothing to reconcile"."""
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-002")
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-002", "FAILED", 101)])

    monkeypatch.chdir(root)
    first = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert second.exit_code == 0, second.output
    assert "nothing to reconcile" in second.output
    assert _status(db, run_id, "99.1-002") == "DONE"  # unchanged, still DONE
    assert _merge_done(db, run_id, "99.1-002") == 1  # no duplicate merge row


def test_reconcile_defaults_to_latest_run(tmp_path: Path, monkeypatch) -> None:
    """With no run id, reconcile targets the most recent run (mirrors rollback)."""
    root = _init_repo_with_origin(tmp_path)
    db = tmp_path / "ledger.db"
    # An older run, then the latest run whose work actually landed.
    old_run = _seed_run(db, [("99.1-003", "FAILED", 102)])
    _land_story(root, "99.1-004")
    latest_run = _seed_run(db, [("99.1-004", "FAILED", 103)])

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert _status(db, latest_run, "99.1-004") == "DONE"  # latest reconciled
    assert _status(db, old_run, "99.1-003") == "FAILED"  # older run untouched


def test_reconcile_explicit_run_id(tmp_path: Path, monkeypatch) -> None:
    """An explicit run id reconciles that run, not the latest."""
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-005")
    db = tmp_path / "ledger.db"
    target = _seed_run(db, [("99.1-005", "FAILED", 104)])
    _seed_run(db, [("99.1-006", "FAILED", 105)])  # a newer, unlanded run

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", target, "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert _status(db, target, "99.1-005") == "DONE"


# --- idempotent "nothing to reconcile" when no parked stories ---------------


def test_reconcile_no_parked_stories_is_noop(tmp_path: Path) -> None:
    """A run with no parked stories needs no git and reports nothing to do."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "99.1-007", "99", "t", "P1", 1, "x", "", None, "TODO"
    )
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-007", stage, 1)
        ledger.stage_finish(run_id, "99.1-007", stage, 1, "DONE")
    ledger.set_story_status(run_id, "99.1-007", "DONE")
    ledger.run_update_status(run_id, "DONE")

    result = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "nothing to reconcile" in result.output


# --- offline / no-remote degrades to a clean skip ---------------------------


def test_reconcile_offline_skips_cleanly(tmp_path: Path, monkeypatch) -> None:
    from test_reconcile import _init_repo

    root = _init_repo(tmp_path)  # no origin remote → git fetch origin fails
    _checkout(root, "feature/99.1-008", new=True)
    _commit(root, "x.py", "x = 1\n", "feat: x (#99.1-008)")
    _checkout(root, "main")
    _git(root, "merge", "-q", "--ff-only", "feature/99.1-008")
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-008", "FAILED", 106)])

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "skip" in result.output.lower()
    assert _status(db, run_id, "99.1-008") == "FAILED"  # left parked


# --- no-DB / unknown-run handling -------------------------------------------


def test_reconcile_no_ledger_is_clean(tmp_path: Path) -> None:
    """No ledger → reports cleanly, exits 0, and creates no spurious DB."""
    db = tmp_path / "nope.db"
    result = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "no build run" in result.output.lower()
    assert not db.exists()  # recovery verb must not materialise an empty ledger


def test_reconcile_unknown_explicit_run_exits_nonzero(tmp_path: Path) -> None:
    """A genuinely-unknown explicit run id is the one non-zero case."""
    db = tmp_path / "ledger.db"
    _seed_run(db, [("99.1-009", "FAILED", 107)])
    result = runner.invoke(
        app, ["reconcile", "no-such-run-id", "--db", str(db)]
    )
    assert result.exit_code != 0
    assert "no-such-run-id" in result.output or "no such run" in result.output.lower()


# --- ensure_migrated runs first (stale ledger does not crash) ---------------


def test_reconcile_migrates_stale_ledger_at_launch(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert _USAGE_COLS <= _columns(db, "stages")  # migrated before any read


# --- the verb is wired, not a stub -----------------------------------------


def test_reconcile_is_listed_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reconcile" in result.output
