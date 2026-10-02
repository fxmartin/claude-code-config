# ABOUTME: Tests for `--enqueue` targeting the fleet queue, with --host/--pool (Story 35.3-001).
# ABOUTME: Payload shape, pin/pool recorded, flag validation and `sdlc queue list` columns, vs a live in-process service.

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sdlc.role_routing as rr
from sdlc.cli import _split_fleet_flags, app
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient, QueueRequestError
from sdlc.queue_server import AccessPolicy, make_server

runner = CliRunner()
TOKEN = "s3cret-token"
ORIGIN = "http://gitlab.test/root/widgets.git"


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
    root = tmp_path / "widgets"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", ORIGIN], check=True)
    monkeypatch.chdir(root)
    return root


def _only_job(store: QueueStore):
    jobs = store.list_jobs()
    assert len(jobs) == 1
    return jobs[0]


def _register_worker(fleet, name: str, host: str, **capabilities) -> None:
    """Register a worker through the live service's real ``POST /workers`` (Story 35.2-001)."""
    QueueClient(fleet.url, token=TOKEN).register_worker(name, host=host, **capabilities)


# --- AC1: payload shape ------------------------------------------------------


def test_build_enqueue_lands_on_fleet_with_requirements(fleet, repo) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--auto"])
    assert result.exit_code == 0, result.output
    assert fleet.url in result.output
    job = _only_job(fleet)
    assert f"job {job.id}" in result.output
    assert (job.kind, job.scope, job.state) == ("build", "12.4-005", "queued")
    assert json.loads(job.options) == ["12.4-005", "--auto"]
    assert json.loads(job.requirements) == {"repo": "widgets", "origin": ORIGIN, "harness": "claude"}
    assert (job.host, job.pool) == (None, None)


def test_requirements_harness_comes_from_flag_and_repo_file(fleet, repo) -> None:
    (repo / ".sdlc-harness.yaml").write_text("harness:\n  default: codex\n  roles:\n    review: claude\n")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--harness", "build=gemini"])
    assert result.exit_code == 0, result.output
    # CLI flag > repo file per role; the set of distinct harnesses the job routes to.
    assert json.loads(_only_job(fleet).requirements)["harness"] == "claude,codex,gemini"


def test_requirements_harness_includes_the_registry_default(fleet, repo, monkeypatch) -> None:
    # The same resolution the run itself does (Issue #551): a registry `default:`
    # fills every role the flag and the repo file leave unnamed.
    monkeypatch.setattr(rr, "registry_default_harness", lambda _path: "codex")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--harness", "review=claude"])
    assert result.exit_code == 0, result.output
    assert json.loads(_only_job(fleet).requirements)["harness"] == "claude,codex"


def test_requirements_record_the_sandbox_flag(fleet, repo) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--sandbox"])
    assert result.exit_code == 0, result.output
    assert json.loads(_only_job(fleet).requirements)["sandbox"] == "container"


def test_fix_enqueue_lands_on_fleet(fleet, repo) -> None:
    result = runner.invoke(app, ["fix", "123", "--enqueue"])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert (job.kind, job.scope) == ("fix", "123")
    assert json.loads(job.requirements)["origin"] == ORIGIN


def test_credentialed_origin_never_reaches_the_queue(fleet, repo) -> None:
    # Epic 35: the queue service never holds credentials — jobs carry no tokens.
    secret = "glpat-SECRET"
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", f"https://oauth2:{secret}@gitlab.test/root/widgets.git"],
        check=True,
    )
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue"])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert json.loads(job.requirements)["origin"] == "https://gitlab.test/root/widgets.git"
    assert secret not in job.requirements
    assert secret not in result.output


def test_origin_less_repo_still_enqueues_without_origin(fleet, tmp_path, monkeypatch) -> None:
    bare = tmp_path / "plain"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = runner.invoke(app, ["fix", "5", "--enqueue"])
    assert result.exit_code == 0, result.output
    assert json.loads(_only_job(fleet).requirements) == {"repo": "plain", "harness": "claude"}


def test_local_enqueue_is_unchanged_without_a_fleet(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue"])
    assert result.exit_code == 0, result.output
    job = _only_job(QueueStore(tmp_path / "queue.db"))
    assert job.requirements is None


# --- AC2: pin / pool recorded ------------------------------------------------


@pytest.mark.parametrize("pin", [["--host", "home-lab"], ["--host=home-lab"]])
def test_host_pin_recorded(fleet, repo, pin) -> None:
    _register_worker(fleet, "lab", "home-lab")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", *pin])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.host == "home-lab"
    assert "home-lab" in result.output
    assert not any(a.startswith("--host") or a == "home-lab" for a in json.loads(job.options))


@pytest.mark.parametrize("pool", [["--pool", "claude-shared"], ["--pool=claude-shared"]])
def test_pool_recorded_for_fix(fleet, repo, pool) -> None:
    result = runner.invoke(app, ["fix", "123", "--enqueue", *pool])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.pool == "claude-shared"
    assert "claude-shared" in result.output
    assert not any(a.startswith("--pool") or a == "claude-shared" for a in json.loads(job.options))


@pytest.mark.parametrize(
    "forge,frozen",
    [(["--host=gitlab"], "--host=gitlab"), (["--host", "gitlab"], "--host=gitlab"), (["--host", "GitHub"], "--host=GitHub")],
)
def test_forge_host_flag_stays_a_forge_flag(fleet, repo, forge, frozen) -> None:
    # The value, not the form, decides: `--host gitlab` is the forge override too.
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", *forge])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.host is None
    assert frozen in json.loads(job.options)


def test_space_form_forge_host_is_not_a_pin(fleet, repo) -> None:
    # Review repro: this queued a job pinned to a machine called "gitlab" (which no
    # worker would ever claim) and silently dropped the forge override.
    result = runner.invoke(app, ["fix", "123", "--enqueue", "--host", "gitlab"])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.host is None
    assert json.loads(job.options) == ["123", "--host=gitlab"]


def test_unknown_host_lists_known_workers(fleet, repo) -> None:
    _register_worker(fleet, "m3max", "macbook-pro-m3-max")
    _register_worker(fleet, "lab", "home-lab")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--host", "nowhere"])
    assert result.exit_code == 2
    assert "nowhere" in result.output
    assert "m3max (host macbook-pro-m3-max)" in result.output and "lab (host home-lab)" in result.output
    assert fleet.list_jobs() == []


def test_pin_names_a_worker_host_not_a_worker_name(fleet, repo) -> None:
    # 35.2-001 matches a pin on the worker's host, so a worker's name is no pin.
    _register_worker(fleet, "lab", "home-lab")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--host=lab"])
    assert result.exit_code == 2
    assert "lab (host home-lab)" in result.output
    assert fleet.list_jobs() == []


def test_empty_registry_rejects_any_pin(fleet, repo) -> None:
    result = runner.invoke(app, ["fix", "1", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 2
    assert "no workers" in result.output.lower()
    assert fleet.list_jobs() == []


def test_service_that_cannot_list_workers_refuses_the_pin(fleet, repo, monkeypatch) -> None:
    # A pin is never accepted unchecked: an unverifiable pin may strand the job.
    def no_registry(self):
        raise QueueRequestError(404, "unknown route: GET /workers")

    monkeypatch.setattr(QueueClient, "list_workers", no_registry)
    result = runner.invoke(app, ["fix", "1", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 2
    assert "GET /workers" in result.output
    assert fleet.list_jobs() == []


# --- flag validation ---------------------------------------------------------


@pytest.mark.parametrize("verb,target", [("build", "12.4-005"), ("fix", "123")])
@pytest.mark.parametrize("flags", [["--pool", "claude-shared"], ["--pool=x"], ["--host", "home-lab"], ["--host=home-lab"]])
def test_fleet_flags_rejected_without_enqueue(fleet, repo, verb, target, flags) -> None:
    result = runner.invoke(app, [verb, target, *flags])
    assert result.exit_code == 2
    assert "--enqueue" in result.output
    assert fleet.list_jobs() == []


def test_pin_without_enqueue_names_the_pin_and_the_forge_override(fleet, repo) -> None:
    # `--host` alone is not enqueue-only (the forge override is not), so say which
    # value was taken as a pin and where the forge override went.
    result = runner.invoke(app, ["fix", "123", "--host", "home-lab"])
    assert result.exit_code == 2
    assert "--enqueue is required for --host home-lab" in result.output
    assert "--host github|gitlab" in result.output
    assert fleet.list_jobs() == []


@pytest.mark.parametrize("forge", [["--host", "gitlab"], ["--host=gitlab"]])
def test_forge_host_needs_no_enqueue(forge) -> None:
    # Handed to the run's own parser in the one form it takes.
    assert _split_fleet_flags(["123", *forge], enqueue=False) == (["123", "--host=gitlab"], None, None)


@pytest.mark.parametrize("flags", [["--pool"], ["--host"], ["--pool="], ["--host="]])
def test_fleet_flag_needs_a_value(fleet, repo, flags) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", *flags])
    assert result.exit_code == 2
    assert "requires a value" in result.output
    assert fleet.list_jobs() == []


def test_pin_without_a_fleet_queue_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue", "--pool", "claude-shared"])
    assert result.exit_code == 2
    assert "SDLC_QUEUE_URL" in result.output
    assert QueueStore(tmp_path / "queue.db").list_jobs() == []


def test_fleet_down_never_falls_back_to_local(tmp_path, monkeypatch, repo) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 2
    assert not (tmp_path / "queue.db").exists()


def test_invalid_repo_harness_file_is_a_parse_error(fleet, repo) -> None:
    (repo / ".sdlc-harness.yaml").write_text("harness:\n  default: [unclosed\n")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue"])
    assert result.exit_code == 2
    assert fleet.list_jobs() == []


# --- AC3: list columns -------------------------------------------------------


def _cells(output: str) -> dict[str, str]:
    """`sdlc queue list`'s first job row, cut at the header's column starts."""
    header, row = output.splitlines()[0:2]
    columns = [(m.group(), m.start()) for m in re.finditer(r"\S+", header)]
    ends = [start for _, start in columns[1:]] + [None]
    return {name: row[start:end].strip() for (name, start), end in zip(columns, ends)}


@pytest.mark.parametrize(
    "worker,host,pool",
    [
        # The M3 Max's 18-character host name ran into POOL: `macbook-pro-m3-maxclaude-shared`.
        ("m3max", "macbook-pro-m3-max", "claude-shared"),
        ("a-worker-name-past-twelve", "a-host-name-past-fourteen", "a-pool-name-past-sixteen"),
    ],
)
def test_queue_list_shows_worker_host_pool_columns(fleet, repo, worker, host, pool) -> None:
    _register_worker(fleet, worker, host, pools=[pool], harnesses=["claude"], repos=["widgets"])
    enqueued = runner.invoke(app, ["fix", "123", "--enqueue", "--host", host, "--pool", pool])
    assert enqueued.exit_code == 0, enqueued.output
    assert fleet.claim_next(claimed_by=worker, lease_seconds=60, host=host, pools=[pool]) is not None
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    cells = _cells(result.output)
    assert (cells["WORKER"], cells["HOST"], cells["POOL"]) == (worker, host, pool)
    assert cells["REPO"] == str(repo.resolve())


def test_queue_list_dashes_empty_fleet_columns(fleet, repo) -> None:
    runner.invoke(app, ["fix", "123", "--enqueue"])
    cells = _cells(runner.invoke(app, ["queue", "list"]).output)
    assert (cells["WORKER"], cells["HOST"], cells["POOL"]) == ("-", "-", "-")
