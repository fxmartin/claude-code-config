# ABOUTME: Tests for the shared run-finalization helper (Story 12.3-004).
# ABOUTME: One finalize_run computes terminal/counts/event/status for build+resume.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import Ledger, finalize_run


def _open(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def _new_run(db: Path) -> tuple[Ledger, str]:
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    return ledger, run_id


def test_finalize_run_computes_counts_and_terminal(tmp_path) -> None:
    """A plain DONE/SKIPPED map closes the run out DONE with reconciled counts."""
    ledger, rid = _new_run(tmp_path / "l.db")
    status = {"a": "DONE", "b": "DONE", "c": "SKIPPED"}

    outcome = finalize_run(ledger, rid, status)

    assert outcome.run_terminal == "DONE"
    assert outcome.completed == 2
    assert outcome.skipped == 1
    assert outcome.failed == 0
    # The run row is stamped terminal with reconciled counts.
    row = _open(tmp_path / "l.db").execute(
        "SELECT status, completed, failed FROM runs"
    ).fetchone()
    assert row == ("DONE", 2, 0)


def test_finalize_run_failed_wins(tmp_path) -> None:
    ledger, rid = _new_run(tmp_path / "l.db")
    outcome = finalize_run(ledger, rid, {"a": "DONE", "b": "FAILED"})
    assert outcome.run_terminal == "FAILED"
    assert outcome.failed == 1


def test_finalize_run_awaiting_approval_terminal(tmp_path) -> None:
    """A run whose only non-DONE story is AWAITING_APPROVAL reports that terminal."""
    ledger, rid = _new_run(tmp_path / "l.db")
    outcome = finalize_run(ledger, rid, {"a": "DONE", "b": "AWAITING_APPROVAL"})
    assert outcome.run_terminal == "AWAITING_APPROVAL"
    assert outcome.awaiting_approval == 1
    status = _open(tmp_path / "l.db").execute("SELECT status FROM runs").fetchone()[0]
    assert status == "AWAITING_APPROVAL"


def test_finalize_run_needs_attention_beats_awaiting(tmp_path) -> None:
    ledger, rid = _new_run(tmp_path / "l.db")
    outcome = finalize_run(
        ledger, rid, {"a": "NEEDS_ATTENTION", "b": "AWAITING_APPROVAL"}
    )
    assert outcome.run_terminal == "NEEDS_ATTENTION"


def test_finalize_run_extra_skipped_folds_in(tmp_path) -> None:
    """Build's pre-loop shipped skips are folded into the tally via extra_skipped."""
    ledger, rid = _new_run(tmp_path / "l.db")
    outcome = finalize_run(ledger, rid, {"a": "DONE"}, extra_skipped=2)
    assert outcome.skipped == 2


def test_finalize_run_logs_finish_event(tmp_path) -> None:
    """The finish event uses the supplied label and suffix at the success level."""
    ledger, rid = _new_run(tmp_path / "l.db")
    finalize_run(
        ledger, rid, {"a": "DONE"},
        finish_label="resume finished", finish_suffix=" (1 resumed)",
    )
    rows = _open(tmp_path / "l.db").execute(
        "SELECT level, message FROM events WHERE source='controller' "
        "AND message LIKE '%finished%'"
    ).fetchall()
    assert rows, "a finish event must be logged"
    level, message = rows[-1]
    assert level == "success"
    assert message.startswith("resume finished:")
    assert message.endswith("(1 resumed)")


def test_finalize_run_invokes_reconcile_when_enabled(tmp_path, monkeypatch) -> None:
    """With reconcile=True the helper reclassifies parked-but-landed stories."""
    ledger, rid = _new_run(tmp_path / "l.db")
    from sdlc.reconcile import ReconcileResult

    calls: list[tuple] = []

    def fake_reconcile(led, run_id, root=None, fetch=True):
        calls.append((run_id, fetch, root))
        return ReconcileResult(
            run_id=run_id,
            reclassified=[{"story_id": "b", "from_status": "FAILED",
                           "signal": "is-ancestor", "sha": "deadbeef"}],
        )

    monkeypatch.setattr("sdlc.reconcile.reconcile_run", fake_reconcile)

    status = {"a": "DONE", "b": "FAILED"}
    outcome = finalize_run(
        ledger, rid, status, reconcile=True, root=tmp_path,
    )

    assert calls and calls[0][1] is True  # ran with fetch=True
    assert calls[0][2] == tmp_path  # honoured the supplied root
    assert status["b"] == "DONE"  # mutated in place for the caller's return map
    assert outcome.run_terminal == "DONE"
    assert outcome.failed == 0


def test_finalize_run_skips_reconcile_when_disabled(tmp_path, monkeypatch) -> None:
    """Injected-fake runs (reconcile=False) must never touch reconcile's git I/O."""
    ledger, rid = _new_run(tmp_path / "l.db")

    def _boom(*_a, **_k):
        raise AssertionError("reconcile must not run when disabled")

    monkeypatch.setattr("sdlc.reconcile.reconcile_run", _boom)
    outcome = finalize_run(ledger, rid, {"a": "DONE"}, reconcile=False)
    assert outcome.run_terminal == "DONE"


def test_finalize_run_survives_reconcile_failure(tmp_path, monkeypatch) -> None:
    """A reconcile that raises never fails an otherwise-good run."""
    ledger, rid = _new_run(tmp_path / "l.db")

    def _raise(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("sdlc.reconcile.reconcile_run", _raise)
    outcome = finalize_run(ledger, rid, {"a": "DONE"}, reconcile=True)
    assert outcome.run_terminal == "DONE"


def test_finalize_run_stamps_registry_when_provided(tmp_path) -> None:
    """The registry finish is best-effort and only fires when a registry is given."""
    ledger, rid = _new_run(tmp_path / "l.db")

    calls: list[tuple] = []

    class FakeRegistry:
        def mark_finished(self, run_id, status, *, completed):
            calls.append((run_id, status, completed))

    finalize_run(ledger, rid, {"a": "DONE", "b": "DONE"}, registry=FakeRegistry())
    assert calls == [(rid, "DONE", 2)]


def test_finalize_run_registry_io_error_is_swallowed(tmp_path) -> None:
    ledger, rid = _new_run(tmp_path / "l.db")

    class BrokenRegistry:
        def mark_finished(self, *a, **k):
            raise OSError("disk full")

    # Must not raise.
    outcome = finalize_run(ledger, rid, {"a": "DONE"}, registry=BrokenRegistry())
    assert outcome.run_terminal == "DONE"


def test_finalize_run_invokes_render_view(tmp_path) -> None:
    ledger, rid = _new_run(tmp_path / "l.db")
    seen: list[str] = []
    finalize_run(ledger, rid, {"a": "DONE"}, render_view=seen.append)
    assert seen == [rid]


def test_finalize_run_identical_terminal_build_vs_resume(tmp_path) -> None:
    """Parity: the same final story map yields the same terminal regardless of the
    caller-specific knobs (registry/label/extra_skipped) build and resume pass."""
    status_map = {"a": "DONE", "b": "AWAITING_APPROVAL", "c": "DONE"}

    led_b, rid_b = _new_run(tmp_path / "build.db")
    build_like = finalize_run(
        led_b, rid_b, dict(status_map),
        registry=None, extra_skipped=0, finish_label="run finished",
    )

    led_r, rid_r = _new_run(tmp_path / "resume.db")
    resume_like = finalize_run(
        led_r, rid_r, dict(status_map),
        finish_label="resume finished", finish_suffix=" (1 resumed)",
    )

    assert build_like.run_terminal == resume_like.run_terminal == "AWAITING_APPROVAL"
    assert build_like.awaiting_approval == resume_like.awaiting_approval == 1
