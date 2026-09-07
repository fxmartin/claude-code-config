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
            # Story 32.2-002's approval park.
            "pr_number", "poll_after",
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


def test_apply_migrations_reruns_a_version_whose_recorded_name_disagrees(
    tmp_path, monkeypatch,
) -> None:
    """Mirrors the build-ledger fix for Issue #621: a queue.db can carry a
    bookkeeping row whose version matches a current migration but whose name
    is stale (e.g. from a renumbering upstream). A version-only idempotency
    check would treat that row as proof the migration already ran and skip
    its column-add forever. The name-aware check must re-run it instead."""
    import sdlc.queue as queue_mod
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    store = QueueStore(db)
    store.init()  # applies the real _MIGRATIONS, recording version 1 as "approval_park"

    fake_migration = (
        1,
        "add_worker_note",
        "jobs",
        [("worker_note", "TEXT")],
        None,
    )
    monkeypatch.setattr(queue_mod, "_MIGRATIONS", [fake_migration])

    store.init()  # version 1's recorded name now disagrees with the (stubbed) definition

    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "worker_note" in cols
        name = conn.execute("SELECT name FROM _migrations WHERE version = 1").fetchone()[0]
        assert name == "add_worker_note"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Story 32.1-003: per-repo exclusivity relaxed for build/build, kept for any
# combination touching a fix job (fix runs in the repo root, exclusive always).
# ---------------------------------------------------------------------------


def _store(tmp_path):
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def test_claim_job_allows_two_build_jobs_in_one_repo(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    first = store.add_job(repo=repo, kind="build", scope="epic-1")
    second = store.add_job(repo=repo, kind="build", scope="epic-2")

    assert store.claim_job(first, claimed_by="w1", lease_seconds=90) is not None
    assert store.claim_job(second, claimed_by="w2", lease_seconds=90) is not None


def test_claim_job_still_exclusive_for_two_fix_jobs_in_one_repo(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    first = store.add_job(repo=repo, kind="fix", scope="1")
    second = store.add_job(repo=repo, kind="fix", scope="2")

    assert store.claim_job(first, claimed_by="w1", lease_seconds=90) is not None
    assert store.claim_job(second, claimed_by="w2", lease_seconds=90) is None


def test_claim_job_fix_blocked_by_a_running_build_in_the_same_repo(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    build_job = store.add_job(repo=repo, kind="build", scope="epic-1")
    fix_job = store.add_job(repo=repo, kind="fix", scope="1")

    assert store.claim_job(build_job, claimed_by="w1", lease_seconds=90) is not None
    assert store.claim_job(fix_job, claimed_by="w2", lease_seconds=90) is None


def test_claim_job_build_blocked_by_a_running_fix_in_the_same_repo(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    fix_job = store.add_job(repo=repo, kind="fix", scope="1")
    build_job = store.add_job(repo=repo, kind="build", scope="epic-1")

    assert store.claim_job(fix_job, claimed_by="w1", lease_seconds=90) is not None
    assert store.claim_job(build_job, claimed_by="w2", lease_seconds=90) is None


def test_peek_claimable_build_not_excluded_by_a_running_build(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    store.add_job(repo=repo, kind="build", scope="epic-2")

    claimable = store.peek_claimable(busy_repos={repo}, fix_busy_repos=set())
    assert len(claimable) == 1


def test_peek_claimable_fix_excluded_by_any_running_job(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    store.add_job(repo=repo, kind="fix", scope="2")

    claimable = store.peek_claimable(busy_repos={repo}, fix_busy_repos=set())
    assert claimable == []


def test_peek_claimable_build_excluded_by_a_running_fix(tmp_path) -> None:
    store = _store(tmp_path)
    repo = str(tmp_path / "repo")
    store.add_job(repo=repo, kind="build", scope="epic-2")

    claimable = store.peek_claimable(busy_repos=set(), fix_busy_repos={repo})
    assert claimable == []


def test_running_repos_filters_by_kind(tmp_path) -> None:
    store = _store(tmp_path)
    build_repo = str(tmp_path / "alpha")
    fix_repo = str(tmp_path / "beta")
    build_job = store.add_job(repo=build_repo, kind="build", scope="epic-1")
    fix_job = store.add_job(repo=fix_repo, kind="fix", scope="1")
    store.claim_job(build_job, claimed_by="w1", lease_seconds=90)
    store.claim_job(fix_job, claimed_by="w2", lease_seconds=90)

    assert store.running_repos() == {build_repo, fix_repo}
    assert store.running_repos(kind="fix") == {fix_repo}
    assert store.running_repos(kind="build") == {build_repo}


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


# --- host-level dispatch pause (Story 32.2-001) ---------------------------


def _paused_store(tmp_path):
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def test_init_creates_the_queue_state_table(tmp_path) -> None:
    """The host pause lives in its own single-row table, created with the schema."""
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    QueueStore(db).init()

    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(queue_state)").fetchall()}
        assert cols == {
            "id", "paused_until", "reason", "run_id", "repo", "source",
            "paused_at", "probed_at",
        }
    finally:
        conn.close()


def test_pre_existing_queue_upgrades_to_the_pause_table(tmp_path) -> None:
    """A queue.db written before this story gains `queue_state` on ensure_migrated."""
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, "
            "kind TEXT, scope TEXT, priority TEXT, state TEXT, claimed_by TEXT, "
            "lease_until TIMESTAMP, run_id TEXT, options TEXT, "
            "created_at TIMESTAMP, updated_at TIMESTAMP, reason TEXT);"
        )
        conn.commit()
    finally:
        conn.close()

    store = QueueStore(db)
    store.ensure_migrated()
    assert store.dispatch_pause() is None  # readable, so the table exists


def test_no_pause_by_default(tmp_path) -> None:
    """A fresh queue is not paused, and an absent store never conjures one."""
    from sdlc.queue import QueueStore

    assert QueueStore(tmp_path / "missing.db").dispatch_pause() is None
    assert _paused_store(tmp_path).dispatch_pause() is None


def test_pause_dispatch_records_the_window_once(tmp_path) -> None:
    """The first pause is *established* (True); a second while it holds is not.

    "Discovered once and waited out once" — the boolean is what gates the single
    notify, so a second rate-limited job inside the same window is silent.
    """
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    until = now + timedelta(seconds=600)

    assert store.pause_dispatch(
        until=until, reason="rate limited", run_id="run-a", repo="/repo/a",
        source="retry-after", now=now,
    ) is True
    assert store.pause_dispatch(
        until=until, reason="rate limited", run_id="run-b", repo="/repo/b", now=now
    ) is False

    pause = store.dispatch_pause()
    assert pause is not None
    assert pause.run_id == "run-a"  # the discovering run keeps the window
    assert pause.source == "retry-after"
    assert pause.is_active(now) is True
    assert pause.is_active(until + timedelta(seconds=1)) is False


def test_pause_dispatch_extends_but_never_shortens_the_window(tmp_path) -> None:
    """A later reset extends the pause; an earlier one must not resume early."""
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    store.pause_dispatch(until=now + timedelta(seconds=600), now=now)

    store.pause_dispatch(until=now + timedelta(seconds=900), now=now)
    assert store.dispatch_pause().paused_until == (now + timedelta(seconds=900)).isoformat()

    store.pause_dispatch(until=now + timedelta(seconds=60), now=now)
    assert store.dispatch_pause().paused_until == (now + timedelta(seconds=900)).isoformat()


def test_pause_dispatch_after_the_window_elapsed_is_a_fresh_discovery(tmp_path) -> None:
    """An elapsed pause no longer holds, so the next limit is a new window."""
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    store.pause_dispatch(until=now + timedelta(seconds=60), run_id="run-a", now=now)

    later = now + timedelta(seconds=120)
    assert store.pause_dispatch(
        until=later + timedelta(seconds=60), run_id="run-b", now=later
    ) is True
    assert store.dispatch_pause().run_id == "run-b"


def test_clear_pause_removes_the_window(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    store.pause_dispatch(until=now + timedelta(seconds=600), now=now)
    store.clear_pause()
    assert store.dispatch_pause() is None


def test_mark_pause_probed_stamps_the_throttle(tmp_path) -> None:
    """The probe throttle is stored, so restarts and peers share one cadence."""
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    store.pause_dispatch(until=now + timedelta(seconds=600), now=now)
    assert store.dispatch_pause().probed_at is None

    store.mark_pause_probed(now=now)
    assert store.dispatch_pause().probed_at == now.isoformat()


def test_pause_to_dict_is_json_safe(tmp_path) -> None:
    """`sdlc queue list --json` and `/api/queue` serialise the pause as-is."""
    from datetime import datetime, timedelta, timezone

    store = _paused_store(tmp_path)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    store.pause_dispatch(
        until=now + timedelta(seconds=600), reason="rate limited",
        run_id="run-a", repo="/repo/a", source="usage-limit", now=now,
    )
    payload = json.loads(json.dumps(store.dispatch_pause().to_dict()))
    assert payload["reason"] == "rate limited"
    assert payload["run_id"] == "run-a"
    assert payload["paused_until"] == (now + timedelta(seconds=600)).isoformat()


def test_pause_is_active_tolerates_a_corrupt_timestamp(tmp_path) -> None:
    """A hand-edited/garbled `paused_until` must not wedge the queue shut."""
    from sdlc.queue import QueuePause

    assert QueuePause(paused_until="not-a-time", paused_at="").is_active() is False


def test_reading_a_pause_from_an_unmigrated_queue_is_not_paused(tmp_path) -> None:
    """`sdlc queue list` on a pre-32.2-001 queue.db reads "not paused", not a crash.

    Read verbs never migrate (and never create), so the pause table can legitimately
    be missing under them — that has to degrade, or the first `queue list` after an
    upgrade would blow up.
    """
    from sdlc.queue import QueueStore

    db = tmp_path / "queue.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, "
            "kind TEXT, scope TEXT, priority TEXT, state TEXT, claimed_by TEXT, "
            "lease_until TIMESTAMP, run_id TEXT, options TEXT, "
            "created_at TIMESTAMP, updated_at TIMESTAMP, reason TEXT);"
        )
        conn.commit()
    finally:
        conn.close()

    assert QueueStore(db).dispatch_pause() is None


def test_clear_pause_on_an_absent_store_is_a_no_op(tmp_path) -> None:
    """Never conjure a queue.db from a write that has nothing to undo."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.clear_pause()
    assert not (tmp_path / "queue.db").exists()


def test_naive_timestamps_are_read_as_utc(tmp_path) -> None:
    """A hand-edited store may carry a tz-naive instant; treat it as UTC.

    Assuming the local zone instead would silently shift the window by hours.
    """
    from datetime import datetime, timedelta, timezone

    from sdlc.queue import QueuePause, QueueStore

    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    naive = QueuePause(paused_until="2026-09-07T12:10:00", paused_at="")
    assert naive.is_active(now) is True
    assert naive.is_active(now + timedelta(minutes=20)) is False

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.pause_dispatch(until=now + timedelta(seconds=600), now=now)
    conn = sqlite3.connect(tmp_path / "queue.db")
    try:
        with conn:
            conn.execute("UPDATE queue_state SET paused_until = '2026-09-07T12:10:00'")
    finally:
        conn.close()
    # An extension is still measured against that naive instant, read as UTC.
    store.pause_dispatch(until=now + timedelta(seconds=1200), now=now)
    assert store.dispatch_pause().paused_until == (now + timedelta(seconds=1200)).isoformat()
