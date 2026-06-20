# ABOUTME: Tests for the host-level run registry (Story 11.2-001).
# ABOUTME: Covers path resolution, atomic/concurrent writes, stale detection, prune.

from __future__ import annotations

import json
import os
import threading

from sdlc.registry import (
    Registry,
    RunRecord,
    default_registry_path,
    derive_state,
    pid_alive,
)

# A pid that is essentially never alive — used to simulate a crashed run.
DEAD_PID = 2**31 - 1


def _record(run_id: str, **over) -> RunRecord:
    base = dict(
        run_id=run_id,
        repo="/repo/a",
        db="/repo/a/.sdlc-state.db",
        scope="all",
        pid=os.getpid(),
        status="IN_PROGRESS",
        started_at="2026-06-20T10:00:00+00:00",
    )
    base.update(over)
    return RunRecord(**base)  # type: ignore[arg-type]


# --- path resolution --------------------------------------------------------


def test_default_path_honors_explicit_env(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(target))
    assert default_registry_path() == target


def test_default_path_uses_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SDLC_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_registry_path() == tmp_path / "state" / "sdlc" / "registry.json"


def test_default_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SDLC_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_registry_path() == tmp_path / ".sdlc" / "registry.json"


# --- register / read --------------------------------------------------------


def test_register_creates_file_and_parent(tmp_path):
    path = tmp_path / "nested" / "registry.json"
    reg = Registry(path)
    reg.register(_record("run-1"))

    assert path.is_file()
    records = reg.records()
    assert [r.run_id for r in records] == ["run-1"]
    assert records[0].scope == "all"
    assert records[0].finished_at is None


def test_register_upserts_by_run_id(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("run-1", scope="all"))
    reg.register(_record("run-1", scope="epic-11"))

    records = reg.records()
    assert len(records) == 1
    assert records[0].scope == "epic-11"


def test_register_stamps_started_at_when_missing(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("run-1", started_at=""))
    assert reg.records()[0].started_at  # non-empty ISO timestamp


def test_records_empty_when_file_missing(tmp_path):
    reg = Registry(tmp_path / "absent.json")
    assert reg.records() == []


def test_records_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not json", encoding="utf-8")
    reg = Registry(path)
    # A corrupt cache must not crash discovery — it degrades to empty.
    assert reg.records() == []


def test_records_skips_malformed_rows(tmp_path):
    """A partial/junk row must not crash discovery — only it is dropped."""
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            [
                {"run_id": "partial"},  # missing required fields
                "not-a-dict",  # wrong element type
                123,  # wrong element type
                _record("good").to_dict(),  # valid
            ]
        ),
        encoding="utf-8",
    )
    reg = Registry(path)
    assert [r.run_id for r in reg.records()] == ["good"]
    # view() walks the same path and must also survive.
    assert [r["run_id"] for r in reg.view()] == ["good"]


def test_register_survives_existing_malformed_rows(tmp_path):
    """A pre-existing junk cache must not crash a new registration (build path)."""
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps([{"run_id": "partial"}, "junk", _record("keep").to_dict()]),
        encoding="utf-8",
    )
    reg = Registry(path)
    reg.register(_record("new"))
    # The non-dict 'junk' is dropped by the read; the partial dict is preserved
    # (writers don't parse it) but discovery skips it.
    ids = {r.run_id for r in reg.records()}
    assert ids == {"keep", "new"}


def test_derive_state_handles_non_int_pid():
    rec = _record("run-1", pid="not-a-pid")  # type: ignore[arg-type]
    assert derive_state(rec) == "DEAD"


def test_prune_drops_malformed_rows(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps([{"run_id": "partial"}, _record("alive", pid=os.getpid()).to_dict()]),
        encoding="utf-8",
    )
    reg = Registry(path)
    removed = reg.prune()
    assert removed == 1  # the partial row
    assert {r.run_id for r in reg.records()} == {"alive"}


# --- mark_finished ----------------------------------------------------------


def test_mark_finished_sets_status_and_timestamp(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("run-1"))
    reg.mark_finished("run-1", "DONE", completed=3)

    rec = reg.records()[0]
    assert rec.status == "DONE"
    assert rec.finished_at
    assert rec.completed == 3


def test_mark_finished_is_noop_for_unknown_run(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("run-1"))
    reg.mark_finished("missing", "DONE")
    rec = reg.records()[0]
    assert rec.status == "IN_PROGRESS"
    assert rec.finished_at is None


# --- stale / dead detection -------------------------------------------------


def test_derive_state_running_for_live_pid():
    rec = _record("run-1", pid=os.getpid())
    assert derive_state(rec) == "IN_PROGRESS"


def test_derive_state_dead_for_dead_pid_without_finish():
    rec = _record("run-1", pid=DEAD_PID)
    assert derive_state(rec) == "DEAD"


def test_derive_state_terminal_when_finished_even_if_pid_dead():
    rec = _record(
        "run-1", pid=DEAD_PID, status="DONE", finished_at="2026-06-20T11:00:00+00:00"
    )
    assert derive_state(rec) == "DONE"


def test_view_annotates_state(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("alive", pid=os.getpid()))
    reg.register(_record("crashed", pid=DEAD_PID))
    states = {row["run_id"]: row["state"] for row in reg.view()}
    assert states == {"alive": "IN_PROGRESS", "crashed": "DEAD"}


def test_pid_alive_self_and_dead():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(DEAD_PID) is False
    assert pid_alive(0) is False


# --- prune ------------------------------------------------------------------


def test_prune_removes_dead_keeps_live_and_finished(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("alive", pid=os.getpid()))
    reg.register(_record("crashed", pid=DEAD_PID))
    reg.register(
        _record(
            "done", pid=DEAD_PID, status="DONE", finished_at="2026-06-20T11:00:00+00:00"
        )
    )

    removed = reg.prune()
    assert removed == 1
    remaining = {r.run_id for r in reg.records()}
    assert remaining == {"alive", "done"}


def test_prune_include_finished_drops_terminal_runs(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.register(_record("alive", pid=os.getpid()))
    reg.register(
        _record(
            "done", pid=DEAD_PID, status="DONE", finished_at="2026-06-20T11:00:00+00:00"
        )
    )
    removed = reg.prune(include_finished=True)
    assert removed == 1
    assert {r.run_id for r in reg.records()} == {"alive"}


# --- concurrency ------------------------------------------------------------


def test_concurrent_registration_loses_no_entries(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    ids = [f"run-{i}" for i in range(25)]

    def worker(rid: str) -> None:
        reg.register(_record(rid))

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {r.run_id for r in reg.records()} == set(ids)


def test_written_file_is_valid_json_list(tmp_path):
    path = tmp_path / "registry.json"
    reg = Registry(path)
    reg.register(_record("run-1"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0]["run_id"] == "run-1"
