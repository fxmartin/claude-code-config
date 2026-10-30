# ABOUTME: Tests for platform-aware fleet matching (Story 35.2-015): a repo declares the
# ABOUTME: platforms its suite passes on, workers advertise theirs, and jobs only meet a match.

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.issue_host import IssueHostError, load_repo_platforms
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient
from sdlc.queue_server import AccessPolicy, make_server
from sdlc.queue_worker import WorkerProfile, detect_worker_profile

runner = CliRunner()
TOKEN = "s3cret-token"
ORIGIN = "http://gitlab.test/root/acm.git"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A live queue service in tmp, with SDLC_QUEUE_URL pointing at it."""
    store = QueueStore(tmp_path / "service-queue.db")
    store.init()
    server = make_server(store, AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",)), "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    store.url = url  # type: ignore[attr-defined]
    yield store
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "acm"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", ORIGIN], check=True)
    monkeypatch.chdir(root)
    return root


def _linux_only(repo: Path) -> None:
    (repo / ".sdlc-forge.yaml").write_text(
        "forge: gitlab\ngitlab_url: http://gitlab.test\nplatforms: [linux]\n"
    )


def _client(fleet) -> QueueClient:
    return QueueClient(fleet.url, token=TOKEN)


def _job(store: QueueStore, platform: str | None) -> int:
    requirements = {"repo": "acm", "harness": "claude"}
    if platform is not None:
        requirements["platform"] = platform
    return store.add_job(
        repo="/r/acm", kind="build", scope="15.1-004", requirements_json=json.dumps(requirements)
    )


def _register(store: QueueStore, name: str, platform: str | None, **overrides):
    caps = {
        "host": name,
        "harnesses": ["claude"],
        "repos": ["acm"],
        "slots": 1,
        "now": T0,
        "platform": platform,
    }
    caps.update(overrides)
    return store.register_worker(name, **caps)


# --- the repo file ------------------------------------------------------------------


def test_platforms_load_from_the_forge_file() -> None:
    text = "forge: gitlab\nplatforms: [linux]\n"
    assert load_repo_platforms(override_text=text) == ["linux"]


def test_platforms_are_lower_cased_and_de_duplicated() -> None:
    assert load_repo_platforms(override_text="forge: gitlab\nplatforms: [Linux, darwin, linux]\n") == [
        "linux",
        "darwin",
    ]


def test_a_single_platform_string_is_accepted() -> None:
    assert load_repo_platforms(override_text="forge: gitlab\nplatforms: linux\n") == ["linux"]


def test_no_file_or_no_key_declares_no_platforms(tmp_path) -> None:
    assert load_repo_platforms(override_path=tmp_path / ".sdlc-forge.yaml") is None
    assert load_repo_platforms(override_text="forge: gitlab\n") is None


@pytest.mark.parametrize(
    "value", ["[windows]", "[]", "[1]", "{linux: true}", "['']"]
)
def test_a_malformed_platforms_key_fails_fast(value: str) -> None:
    with pytest.raises(IssueHostError, match="platforms"):
        load_repo_platforms(override_text=f"forge: gitlab\nplatforms: {value}\n")


# --- matching -------------------------------------------------------------------------


def test_a_worker_records_its_advertised_platform(store) -> None:
    record = _register(store, "m3max", "Darwin")
    assert record.platform == "darwin"
    assert store.get_worker("m3max").platform == "darwin"


def test_a_darwin_worker_skips_a_linux_job_and_the_reason_names_the_need(store) -> None:
    _register(store, "m3max", "darwin")
    job_id = _job(store, "linux")

    assert store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0) is None

    job = store.get_job(job_id)
    assert job.state == "queued"
    assert job.reason == "no eligible worker (needs platform linux)"


def test_a_linux_worker_claims_the_linux_job(store) -> None:
    _register(store, "m3max", "darwin")
    _register(store, "xps", "linux")
    job_id = _job(store, "linux")

    assert store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0) is None
    claimed = store.claim_next(claimed_by="xps", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_multi_platform_job_runs_on_any_of_them(store) -> None:
    _register(store, "m3max", "darwin")
    job_id = _job(store, "darwin,linux")

    claimed = store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_worker_that_never_advertised_a_platform_may_run_any_job(store) -> None:
    _register(store, "old", None)
    job_id = _job(store, "linux")

    claimed = store.claim_next(claimed_by="old", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_job_without_a_platform_runs_anywhere(store) -> None:
    _register(store, "m3max", "darwin")
    job_id = _job(store, None)

    claimed = store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_migration_adds_the_platform_column_to_an_old_workers_table(tmp_path) -> None:
    db = tmp_path / "queue.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE workers (
            name TEXT PRIMARY KEY, host TEXT NOT NULL, pools TEXT NOT NULL DEFAULT '[]',
            harnesses TEXT NOT NULL DEFAULT '[]', sandbox TEXT, repos TEXT NOT NULL DEFAULT '[]',
            slots INTEGER NOT NULL DEFAULT 1, slots_free INTEGER NOT NULL DEFAULT 1,
            registered_at TIMESTAMP NOT NULL, last_heartbeat TIMESTAMP NOT NULL
        );
        INSERT INTO workers(name, host, registered_at, last_heartbeat)
            VALUES ('old', 'm3', '2026-10-30T12:00:00+00:00', '2026-10-30T12:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = QueueStore(db)
    store.ensure_migrated()
    store.ensure_migrated()

    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(workers)")}
        names = [r[0] for r in conn.execute("SELECT name FROM _migrations")]
    finally:
        conn.close()
    assert "platform" in cols
    assert names.count("worker_platform") == 1
    assert store.get_worker("old").platform is None


# --- the worker advertises it -------------------------------------------------------


def test_the_profile_detects_the_platform_lower_cased(tmp_path) -> None:
    profile = detect_worker_profile(
        "m3max", work_dir=tmp_path, probe=lambda tool: False, runtime=lambda: "podman",
        system=lambda: "Darwin",
    )
    assert profile.platform == "darwin"


def test_the_profile_registers_its_platform(store) -> None:
    record = WorkerProfile(name="xps", host="xps", platform="linux").register_with(
        store, slots=1, slots_free=1
    )
    assert record.platform == "linux"


def test_the_platform_rides_the_fleet_heartbeat(fleet) -> None:
    client = _client(fleet)
    record = client.register_worker("xps", host="xps", platform="linux")
    assert record.platform == "linux"
    assert [w.platform for w in client.list_workers()] == ["linux"]
    assert fleet.get_worker("xps").platform == "linux"


def test_queue_workers_shows_the_platform(store, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(store.db_path))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    now = datetime.now(UTC)
    _register(store, "m3max", "darwin", now=now)
    _register(store, "old", None, now=now)

    result = runner.invoke(app, ["queue", "workers"])

    assert result.exit_code == 0, result.output
    header, *rows = result.output.splitlines()
    assert header.split()[2] == "PLATFORM"
    lines = {line.split()[0]: line.split() for line in rows}
    assert lines["m3max"][2] == "darwin"
    assert lines["old"][2] == "-"

    listed = runner.invoke(app, ["queue", "workers", "--json"])
    assert {w["name"]: w["platform"] for w in json.loads(listed.output)["workers"]} == {
        "m3max": "darwin",
        "old": None,
    }


def test_queue_list_names_the_unmet_platform(store, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(store.db_path))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    _register(store, "m3max", "darwin", now=datetime.now(UTC))
    _job(store, "linux")
    store.stamp_unsatisfiable()

    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0, result.output
    assert "no eligible worker (needs platform linux)" in result.output


# --- enqueue ----------------------------------------------------------------------------


@pytest.mark.parametrize("verb,target", [("build", "15.1-004"), ("fix", "12")])
def test_enqueue_records_the_repo_platforms(fleet, repo, verb, target) -> None:
    _linux_only(repo)
    _client(fleet).register_worker("xps", host="xps", platform="linux")

    result = runner.invoke(app, [verb, target, "--enqueue"])

    assert result.exit_code == 0, result.output
    (job,) = fleet.list_jobs()
    assert json.loads(job.requirements)["platform"] == "linux"
    assert "warning" not in result.output.lower()


def test_enqueue_without_platforms_records_none(fleet, repo) -> None:
    (repo / ".sdlc-forge.yaml").write_text("forge: gitlab\n")
    result = runner.invoke(app, ["build", "15.1-004", "--enqueue"])
    assert result.exit_code == 0, result.output
    (job,) = fleet.list_jobs()
    assert "platform" not in json.loads(job.requirements)


def test_a_pin_to_a_mismatched_worker_is_refused_before_recording(fleet, repo) -> None:
    _linux_only(repo)
    client = _client(fleet)
    client.register_worker("m3max", host="m3max", platform="darwin")
    client.register_worker("xps", host="omarchy-xps13", platform="linux")

    result = runner.invoke(app, ["build", "15.1-004", "--enqueue", "--host", "m3max"])

    assert result.exit_code == 2
    assert fleet.list_jobs() == []
    out = " ".join(result.output.split())
    assert "linux" in out and "darwin" in out
    assert "sdlc build 15.1-004 --auto" in out
    assert "omarchy-xps13" in out


def test_a_pin_to_a_worker_without_a_platform_is_accepted(fleet, repo) -> None:
    _linux_only(repo)
    _client(fleet).register_worker("lab", host="home-lab")

    result = runner.invoke(app, ["fix", "12", "--enqueue", "--host", "home-lab"])

    assert result.exit_code == 0, result.output
    (job,) = fleet.list_jobs()
    assert job.host == "home-lab"


def test_a_pin_to_a_matching_worker_is_accepted(fleet, repo) -> None:
    _linux_only(repo)
    _client(fleet).register_worker("xps", host="omarchy-xps13", platform="linux")

    result = runner.invoke(app, ["build", "15.1-004", "--enqueue", "--host", "omarchy-xps13"])

    assert result.exit_code == 0, result.output
    assert len(fleet.list_jobs()) == 1


def test_no_matching_worker_still_records_and_warns(fleet, repo) -> None:
    _linux_only(repo)
    _client(fleet).register_worker("m3max", host="m3max", platform="darwin")

    result = runner.invoke(app, ["build", "15.1-004", "--enqueue"])

    assert result.exit_code == 0, result.output
    (job,) = fleet.list_jobs()
    assert job.state == "queued"
    out = " ".join(result.output.split())
    assert "warning" in out.lower() and "linux" in out


def test_a_malformed_platforms_key_refuses_the_enqueue(fleet, repo) -> None:
    (repo / ".sdlc-forge.yaml").write_text("forge: gitlab\nplatforms: [windows]\n")

    result = runner.invoke(app, ["build", "15.1-004", "--enqueue"])

    assert result.exit_code == 2
    assert "platforms" in result.output
    assert fleet.list_jobs() == []
