# ABOUTME: Tests for the `sdlc reconcile` recovery verb CLI wiring (Story 12.3-002).
# ABOUTME: Builds a real repo with a local origin, parks a landed story, reconciles.

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

runner = CliRunner()


# --- git + ledger fixtures --------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _repo_with_origin(tmp_path: Path) -> Path:
    """A repo on ``main`` with a local bare ``origin`` so ``git fetch`` works offline."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Isolate from any global hooks (e.g. a gitleaks pre-commit).
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


def _repo_no_origin(tmp_path: Path) -> Path:
    """A repo on ``main`` with NO ``origin`` remote, so ``git fetch origin`` fails."""
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
    return root


def _land_story(root: Path, story_id: str) -> None:
    """Cut ``feature/<id>``, commit, fast-forward into main, and push to origin."""
    _git(root, "checkout", "-q", "-b", f"feature/{story_id}")
    (root / f"{story_id}.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", f"{story_id}.py")
    _git(root, "commit", "-q", "-m", f"feat: work (#{story_id})")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", f"feature/{story_id}")
    _git(root, "push", "-q", "origin", "main")


def _seed_failed_run(db_path: Path, story_id: str, pr: int | None = 100) -> str:
    """A FAILED run with one parked story (build+review DONE, no merge row)."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, story_id, "99", story_id, "P1", 1, "general-purpose", "", None, "TODO"
    )
    for stage in ("build", "review"):
        ledger.stage_start(run_id, story_id, stage, 1)
        ledger.stage_finish(run_id, story_id, stage, 1, "DONE")
    if pr is not None:
        ledger.set_story_pr(run_id, story_id, pr)
    ledger.set_story_status(run_id, story_id, "FAILED")
    ledger.run_update_status(run_id, "FAILED")
    return run_id


def _status(db_path: Path, run_id: str, story_id: str) -> str:
    return {r["story_id"]: r["status"] for r in Ledger(db_path).story_rows(run_id)}[
        story_id
    ]


# --- 429-aborted-then-merged fixture reconciles FAILED → DONE ---------------


def test_reconcile_flips_failed_to_done(tmp_path: Path, monkeypatch) -> None:
    root = _repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    db = root / ".sdlc-state.db"
    run_id = _seed_failed_run(db, "99.1-001")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["reconcile", run_id, "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "DONE" in result.output
    assert "99.1-001" in result.output
    assert "FAILED" in result.output
    assert _status(db, run_id, "99.1-001") == "DONE"
    assert "not yet implemented" not in result.output


# --- defaults to the most recent run ----------------------------------------


def test_reconcile_defaults_to_latest_run(tmp_path: Path, monkeypatch) -> None:
    root = _repo_with_origin(tmp_path)
    _land_story(root, "99.1-002")
    db = root / ".sdlc-state.db"
    run_id = _seed_failed_run(db, "99.1-002")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert run_id[:8] in result.output
    assert _status(db, run_id, "99.1-002") == "DONE"


# --- idempotent re-run reports "nothing to reconcile" -----------------------


def test_reconcile_idempotent_rerun(tmp_path: Path, monkeypatch) -> None:
    root = _repo_with_origin(tmp_path)
    _land_story(root, "99.1-003")
    db = root / ".sdlc-state.db"
    run_id = _seed_failed_run(db, "99.1-003")
    monkeypatch.chdir(root)

    first = runner.invoke(app, ["reconcile", run_id, "--db", str(db)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["reconcile", run_id, "--db", str(db)])
    assert second.exit_code == 0, second.output
    assert "nothing to reconcile" in second.output.lower()


# --- no ledger / no run is a clean exit 0, no spurious DB created ------------


def test_reconcile_no_db_is_clean_noop(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "no build run" in result.output.lower()
    assert not db.exists()  # a read/recovery verb must not create an empty ledger


# --- an unknown explicit run id is the only non-zero exit -------------------


def test_reconcile_unknown_run_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).init()  # ledger exists, but the requested run does not
    result = runner.invoke(app, ["reconcile", "does-not-exist", "--db", str(db)])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output


# --- offline fetch degrades to a clean "skipped" no-op (exit 0) -------------


def test_reconcile_offline_fetch_is_skipped(tmp_path: Path, monkeypatch) -> None:
    root = _repo_no_origin(tmp_path)  # no origin remote → `git fetch origin` fails
    db = root / ".sdlc-state.db"
    run_id = _seed_failed_run(db, "99.1-004")  # a parked story exists to reconcile
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["reconcile", run_id, "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "skipped" in result.output.lower()
    assert "fetch failed" in result.output.lower()
    # A skip leaves the parked story exactly as it was — no spurious flip.
    assert _status(db, run_id, "99.1-004") == "FAILED"


# --- not a stub -------------------------------------------------------------


def test_reconcile_is_not_a_stub(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["reconcile", "--db", str(db)])
    assert "not yet implemented" not in result.output
