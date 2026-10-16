# ABOUTME: Tests for Story 35.3-002 — a job carries its stories' issue mappings to the worker.
# ABOUTME: Enqueue records them, the store/service round-trip them, the worker seeds its clone's ledger.

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc import issue_host as ih
from sdlc.build import BuildOptions, Ledger, _open_story_cr
from sdlc.cli import app
from sdlc.cohort import Story
from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient
from sdlc.queue_server import AccessPolicy, make_server
from sdlc.queue_worker import WorkerProfile, seed_issue_mappings
from sdlc.registry import Registry
from sdlc.scheduler import SchedulerConfig, run_queue

runner = CliRunner()

_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 99.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""

MAPPING = {"99.1-001": ["gitlab", "7"], "99.1-002": ["gitlab", "8"]}


def _project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_EPIC, encoding="utf-8")
    return tmp_path


def _mirror(root: Path, story_id: str, host: str, ref: str) -> None:
    """What `sdlc issues init` leaves in the enqueuing host's ledger."""
    ledger = Ledger(root / ".sdlc-state.db")
    ledger.init()
    ledger.inventory_upsert_specs([(story_id, "99", "99.1", "t", 1, "Low")])
    ledger.inventory_set_mapping(story_id, host, ref)


# --- the store: an additive `issues` column ------------------------------------


def test_the_store_round_trips_the_issues_a_job_carries(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(
        repo="/r", kind="build", scope="epic-99", issues_json=json.dumps(MAPPING)
    )

    job = store.get_job(job_id)
    assert job is not None
    assert json.loads(job.issues or "") == MAPPING
    assert json.loads(job.to_dict()["issues"]) == MAPPING


def test_a_job_without_issues_reads_none(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo="/r", kind="build", scope="epic-99")

    job = store.get_job(job_id)
    assert job is not None and job.issues is None and job.to_dict()["issues"] is None


def test_the_migration_adds_issues_to_a_queue_db_written_before_it(tmp_path) -> None:
    db = tmp_path / "queue.db"
    QueueStore(db).init()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE jobs DROP COLUMN issues")
    conn.execute("DELETE FROM _migrations WHERE name = 'job_issues'")
    conn.execute("INSERT INTO jobs(repo, kind, scope) VALUES ('/old', 'build', 'epic-1')")
    conn.commit()
    conn.close()

    store = QueueStore(db)
    store.init()  # applies the pending migration in place
    store.init()  # and is idempotent

    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    conn.close()
    assert "issues" in cols
    (old,) = store.list_jobs()
    assert old.repo == "/old" and old.issues is None


# --- the service: the mapping rides the HTTP API -------------------------------


@pytest.fixture
def live(tmp_path):
    store = QueueStore(tmp_path / "service.db")
    store.init()
    policy = AccessPolicy(token="tok", networks=("127.0.0.0/8",))
    server = make_server(store, policy, "127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    client = QueueClient(f"http://127.0.0.1:{server.server_address[1]}", token="tok")
    yield client, store
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def test_a_fleet_job_carries_issues_through_the_service(live) -> None:
    client, store = live
    job_id = client.add_job(
        repo="/r", kind="build", scope="epic-99", issues_json=json.dumps(MAPPING)
    )

    [listed] = client.list_jobs()
    assert listed.id == job_id and json.loads(listed.issues or "") == MAPPING
    stored = store.get_job(job_id)
    assert stored is not None and json.loads(stored.issues or "") == MAPPING


@pytest.mark.parametrize(
    "bad",
    [["x"], {"99.1-001": "gitlab#7"}, {"99.1-001": ["gitlab"]}, {"99.1-001": ["gitlab", 7]}],
)
def test_the_service_refuses_a_malformed_issues_object(live, bad) -> None:
    client, _ = live
    with pytest.raises(Exception, match="issues"):
        client.add_job(repo="/r", kind="build", scope="s", issues_json=json.dumps(bad))


# --- enqueue: each scoped story's mapping lands on the job ---------------------


def _enqueue(tmp_path, monkeypatch, *scope: str):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["build", *scope, "--enqueue"])
    assert result.exit_code == 0, result.output
    (job,) = QueueStore(tmp_path / "queue.db").list_jobs()
    return job


def test_enqueue_records_the_mapping_of_each_scoped_story(tmp_path, monkeypatch) -> None:
    _project(tmp_path)
    _mirror(tmp_path, "99.1-001", "gitlab", "7")
    _mirror(tmp_path, "99.1-002", "gitlab", "8")

    job = _enqueue(tmp_path, monkeypatch, "99.1-001", "99.1-002")

    assert json.loads(job.issues or "") == MAPPING


def test_enqueue_carries_only_the_scoped_stories(tmp_path, monkeypatch) -> None:
    _project(tmp_path)
    _mirror(tmp_path, "99.1-001", "gitlab", "7")
    _mirror(tmp_path, "99.1-002", "gitlab", "8")

    job = _enqueue(tmp_path, monkeypatch, "99.1-002")

    assert json.loads(job.issues or "") == {"99.1-002": ["gitlab", "8"]}


def test_enqueue_skips_a_story_with_no_mapping(tmp_path, monkeypatch) -> None:
    _project(tmp_path)
    _mirror(tmp_path, "99.1-001", "gitlab", "7")

    job = _enqueue(tmp_path, monkeypatch, "epic-99")

    assert json.loads(job.issues or "") == {"99.1-001": ["gitlab", "7"]}


def test_enqueue_from_an_unmirrored_host_carries_nothing(tmp_path, monkeypatch) -> None:
    _project(tmp_path)  # no ledger at all

    job = _enqueue(tmp_path, monkeypatch, "epic-99")

    assert job.issues is None
    assert not (tmp_path / ".sdlc-state.db").exists()  # enqueue never conjures a ledger


def test_queue_list_json_shows_the_mapping(tmp_path, monkeypatch) -> None:
    _project(tmp_path)
    _mirror(tmp_path, "99.1-001", "gitlab", "7")
    _enqueue(tmp_path, monkeypatch, "99.1-001")

    result = runner.invoke(app, ["queue", "list", "--json"])

    assert result.exit_code == 0, result.output
    [job] = json.loads(result.output)["jobs"]
    assert json.loads(job["issues"]) == {"99.1-001": ["gitlab", "7"]}


# --- the worker: seed the clone's ledger before the first stage ----------------


def _ledger_at(root: Path) -> Ledger:
    return Ledger(root / ".sdlc-state.db")


def test_seeding_writes_each_mapping_into_a_fresh_clone_ledger(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    job = QueueStore(tmp_path / "q.db")
    job.init()
    job_id = job.add_job(repo=str(clone), kind="build", scope="s", issues_json=json.dumps(MAPPING))

    seed_issue_mappings(job.get_job(job_id))

    ledger = _ledger_at(clone)
    assert ledger.inventory_get_mapping("99.1-001") == ("gitlab", "7")
    assert ledger.inventory_get_mapping("99.1-002") == ("gitlab", "8")
    assert ledger.inventory_any_mapped()


def test_seeding_keeps_the_spec_columns_a_later_projection_writes(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    store = QueueStore(tmp_path / "q.db")
    store.init()
    job_id = store.add_job(repo=str(clone), kind="build", scope="s", issues_json=json.dumps(MAPPING))
    seed_issue_mappings(store.get_job(job_id))

    ledger = _ledger_at(clone)
    ledger.inventory_upsert_specs([("99.1-001", "99", "99.1", "One", 1, "Low")])

    assert ledger.inventory_get_mapping("99.1-001") == ("gitlab", "7")


def test_seeding_a_job_without_issues_touches_nothing(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    store = QueueStore(tmp_path / "q.db")
    store.init()
    job_id = store.add_job(repo=str(clone), kind="build", scope="s")

    seed_issue_mappings(store.get_job(job_id))

    assert not (clone / ".sdlc-state.db").exists()


@pytest.mark.parametrize("junk", ["not json", "[1]", '{"99.1-001": "x"}', '{"99.1-001": ["a"]}'])
def test_seeding_junk_never_raises(tmp_path, junk) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    store = QueueStore(tmp_path / "q.db")
    store.init()
    job_id = store.add_job(repo=str(clone), kind="build", scope="s", issues_json=junk)

    seed_issue_mappings(store.get_job(job_id))  # best-effort: a build is never blocked

    assert not (clone / ".sdlc-state.db").exists()


class _Proc:
    pid = 91001

    def poll(self):
        return 0

    def stop(self) -> None: ...


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def test_the_scheduler_seeds_the_clone_before_launching_the_run(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.add_job(repo=str(clone), kind="build", scope="99.1-001", issues_json=json.dumps(MAPPING))

    seen_at_launch: list[tuple[str, str] | None] = []

    def launcher(argv, cwd):
        seen_at_launch.append(_ledger_at(Path(cwd)).inventory_get_mapping("99.1-001"))
        return _Proc()

    clock = _Clock()
    profile = WorkerProfile(
        name="home-lab", host="home-lab", pools=[], harnesses=["claude"], sandbox=None, repos=[]
    )
    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=profile),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="home-lab",
    )

    assert seen_at_launch == [("gitlab", "7")]


# --- the payoff: the change request opens with `Closes #N` on a fresh clone ----


class _FakeAdapter:
    def __init__(self) -> None:
        self.host = "gitlab"
        self.created: list[dict] = []

    def close_keyword(self, ref: str) -> str:
        return f"Closes #{ref}"

    def issue_find(self, marker: str):  # a host call here would mean the mapping was ignored
        raise AssertionError("no marker search expected when the job carried the mapping")

    def cr_create(self, source_branch, title, body, target_branch=None, draft=False):
        self.created.append({"title": title, "body": body})
        return ih.ChangeRequest(host=self.host, ref="5", url="https://x/5")


def test_a_fresh_clone_opens_its_change_request_with_the_close_link(
    tmp_path, monkeypatch
) -> None:
    import subprocess

    from sdlc import build_issue

    clone = tmp_path / "clone"
    bare = tmp_path / "origin.git"
    clone.mkdir()

    def git(*args: str, cwd: Path = clone) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "--bare", str(bare), cwd=tmp_path)
    git("init", "-b", "main")
    git("config", "user.email", "t@e.c")
    git("config", "user.name", "T")
    (clone / "README.md").write_text("x\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")
    git("remote", "add", "origin", str(bare))
    git("checkout", "-b", "feature/99.1-001")
    (clone / ".sdlc-forge.yaml").write_text(
        "forge: gitlab\ngitlab_url: http://gitlab.test\n", encoding="utf-8"
    )
    store = QueueStore(tmp_path / "q.db")
    store.init()
    job_id = store.add_job(repo=str(clone), kind="build", scope="s", issues_json=json.dumps(MAPPING))
    seed_issue_mappings(store.get_job(job_id))

    fake = _FakeAdapter()
    monkeypatch.chdir(clone)
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None, instance_url=None: fake)
    build_issue._RECOVERED_REFS.clear()
    ledger = _ledger_at(clone)
    story = Story(
        "99.1-001", "One", "99", "x", "epic-99.md", "P1", 1, "py", [], False
    )

    link = build_issue.close_link(ledger, story.id)
    assert link == "Closes #7"

    _open_story_cr(
        story, ledger, "run-1", clone, "origin/main", link, ih.GITLAB_CR_TERMS,
        BuildOptions(), body="Coverage gate passed.", context="post-coverage",
    )
    assert fake.created and "Closes #7" in fake.created[0]["body"]
