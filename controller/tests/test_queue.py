# ABOUTME: Unit tests for the host-level development queue store (Story 32.1-001).
# ABOUTME: Covers path resolution, schema/migration bookkeeping, and job lifecycle verbs.

from __future__ import annotations

import json
import sqlite3

import pytest


def test_default_queue_path_explicit_env(tmp_path, monkeypatch) -> None:
    """`SDLC_QUEUE_PATH` wins over every other resolution source."""
    from sdlc.queue import default_queue_path

    explicit = tmp_path / "somewhere" / "queue.db"
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(explicit))
    assert default_queue_path() == explicit


def test_default_queue_path_xdg_state_home(tmp_path, monkeypatch) -> None:
    """With no explicit path, resolves under `$XDG_STATE_HOME/sdlc/queue.db`."""
    from sdlc.queue import default_queue_path

    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_queue_path() == tmp_path / "sdlc" / "queue.db"


def test_default_queue_path_home_fallback(tmp_path, monkeypatch) -> None:
    """With neither env var set, falls back to `~/.sdlc/queue.db` (registry's sibling)."""
    from sdlc.queue import default_queue_path

    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert default_queue_path() == tmp_path / ".sdlc" / "queue.db"


def test_init_creates_wal_schema(tmp_path) -> None:
    """`QueueStore.init()` creates the `jobs` table in WAL mode."""
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    QueueStore(db).init()
    assert db.exists()

    conn = sqlite3.connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert cols == {
            "id", "repo", "kind", "scope", "priority", "state", "claimed_by",
            "lease_until", "run_id", "options", "created_at", "updated_at", "reason",
            # Story 32.3-001: the frozen per-class budget, the investigated
            # file set the repo-scoped overlap graph is built from, and the
            # fix rounds already banked when the breaker last parked the job.
            "budget", "files", "fix_rounds_baseline",
        }
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path) -> None:
    """Calling `init()` twice does not error and does not duplicate rows."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo="/repo", kind="build", scope="epic-1")
    store.init()
    assert len(store.list_jobs()) == 1


def test_ensure_migrated_noop_when_absent(tmp_path) -> None:
    """`ensure_migrated()` never creates the DB — a read verb must not conjure one."""
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    QueueStore(db).ensure_migrated()
    assert not db.exists()


def test_add_job_records_shape(tmp_path) -> None:
    """A fresh job is `queued`, carries the given fields, and has no run yet."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo="/abs/repo", kind="build", scope="epic-33", priority="high",
        options_json=json.dumps(["epic-33", "--auto"]),
    )
    jobs = store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == job_id
    assert job.repo == "/abs/repo"
    assert job.kind == "build"
    assert job.scope == "epic-33"
    assert job.priority == "high"
    assert job.state == "queued"
    assert job.run_id is None
    assert json.loads(job.options) == ["epic-33", "--auto"]
    assert job.created_at


def test_add_job_defaults_priority_from_kind(tmp_path) -> None:
    """Story 32.3-001: an omitted class is derived, not a flat `normal`."""
    from sdlc.queue import QueueStore, default_priority

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo="/repo", kind="fix", scope="42")
    assert store.list_jobs()[0].priority == default_priority("fix")


def test_add_job_rejects_unknown_kind(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    with pytest.raises(QueueError):
        store.add_job(repo="/repo", kind="frobnicate", scope="all")


def test_add_job_rejects_unknown_priority(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    with pytest.raises(QueueError):
        store.add_job(repo="/repo", kind="build", scope="all", priority="asap")


def test_list_jobs_across_repos(tmp_path) -> None:
    """`list_jobs()` with no filter returns every job on the host, any repo."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo="/repo-a", kind="build", scope="epic-1")
    store.add_job(repo="/repo-b", kind="fix", scope="7")
    repos = {job.repo for job in store.list_jobs()}
    assert repos == {"/repo-a", "/repo-b"}


def test_list_jobs_filters_by_repo(tmp_path) -> None:
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo="/repo-a", kind="build", scope="epic-1")
    store.add_job(repo="/repo-b", kind="fix", scope="7")
    jobs = store.list_jobs(repo="/repo-a")
    assert [j.repo for j in jobs] == ["/repo-a"]


def test_list_jobs_orders_by_priority_then_fifo(tmp_path) -> None:
    """Higher-priority jobs sort first; same-priority jobs stay FIFO."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    first = store.add_job(repo="/r", kind="build", scope="a", priority="normal")
    store.add_job(repo="/r", kind="build", scope="b", priority="low")
    urgent = store.add_job(repo="/r", kind="build", scope="c", priority="urgent")
    second_normal = store.add_job(repo="/r", kind="build", scope="d", priority="normal")

    ids_in_order = [j.id for j in store.list_jobs()]
    assert ids_in_order == [urgent, first, second_normal, ids_in_order[3]]
    assert ids_in_order[3] == store.list_jobs()[3].id  # low sorts last


def test_cancel_marks_queued_job_cancelled(tmp_path) -> None:
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/repo", kind="build", scope="epic-1")
    store.cancel_job(job_id)
    assert store.list_jobs()[0].state == "cancelled"


def test_cancel_refuses_running_job(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/repo", kind="build", scope="epic-1")
    store._set_state(job_id, "running")
    with pytest.raises(QueueError):
        store.cancel_job(job_id)
    assert store.list_jobs()[0].state == "running"


def test_cancel_unknown_id_raises(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    with pytest.raises(QueueError):
        store.cancel_job(999)


def test_prioritise_updates_priority(tmp_path) -> None:
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/repo", kind="build", scope="epic-1")
    store.prioritise_job(job_id, "urgent")
    assert store.list_jobs()[0].priority == "urgent"


def test_prioritise_rejects_unknown_class(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/repo", kind="build", scope="epic-1")
    with pytest.raises(QueueError):
        store.prioritise_job(job_id, "asap")


def test_prioritise_unknown_id_raises(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    with pytest.raises(QueueError):
        store.prioritise_job(999, "high")


def test_counts_by_state(tmp_path) -> None:
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    a = store.add_job(repo="/repo", kind="build", scope="a")
    store.add_job(repo="/repo", kind="build", scope="b")
    store.cancel_job(a)
    counts = store.counts_by_state()
    assert counts == {"queued": 1, "cancelled": 1}


def test_counts_by_state_absent_store_returns_empty(tmp_path) -> None:
    """A store that has never been `init()`-ed reports no counts, not a crash."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    assert store.counts_by_state() == {}


def test_set_state_rejects_unknown_state(tmp_path) -> None:
    from sdlc.queue import QueueError, QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/repo", kind="build", scope="epic-1")
    with pytest.raises(QueueError):
        store._set_state(job_id, "bogus")


def test_apply_migrations_adds_column_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """A migration entry adds its column on first `init()` and is skipped as
    already-applied on a second. Stubbed rather than run against the real
    `_MIGRATIONS` so the mechanism is pinned independently of whichever columns
    the current schema happens to have."""
    import sdlc.queue as queue_mod
    from sdlc.queue import QueueStore

    fake_migration = (
        1,
        "add_worker_note",
        "jobs",
        [("worker_note", "TEXT")],
        "CREATE TABLE IF NOT EXISTS _queue_migration_marker (id INTEGER);",
    )
    monkeypatch.setattr(queue_mod, "_MIGRATIONS", [fake_migration])

    db = tmp_path / "queue.db"
    store = QueueStore(db)
    store.init()

    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "worker_note" in cols
        applied = {row[0] for row in conn.execute("SELECT version FROM _migrations").fetchall()}
        assert applied == {1}
    finally:
        conn.close()

    # A second init() must skip the already-applied version (no duplicate
    # ALTER TABLE, which would raise "duplicate column name").
    store.init()


def test_row_to_record_reads_a_pre_32_3_001_row_without_crashing(tmp_path) -> None:
    """A jobs table from before Story 32.3-001 has no `budget`/`files` columns;
    `_optional_column` must degrade that absence to `None` rather than raising."""
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
            "kind TEXT NOT NULL, scope TEXT NOT NULL, "
            "priority TEXT NOT NULL DEFAULT 'normal', "
            "state TEXT NOT NULL DEFAULT 'queued', claimed_by TEXT, "
            "lease_until TIMESTAMP, run_id TEXT, options TEXT, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, reason TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs(repo, kind, scope, priority, state, created_at, updated_at) "
            "VALUES ('/repo', 'build', '1', 'normal', 'queued', '', '')"
        )
        conn.commit()
    finally:
        conn.close()

    job = QueueStore(db).get_job(1)
    assert job.budget is None
    assert job.files is None
    assert job.fix_rounds_baseline == 0
