# ABOUTME: Tests for worker capability detection (Story 35.2-001) — what a worker advertises.
# ABOUTME: Harness probes, container runtime, and the clones under the work directory.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.dispatch import SandboxUnavailableError
from sdlc.queue_worker import detect_worker_profile


def _clone(work: Path, name: str, *, git_file: bool = False) -> None:
    (work / name).mkdir(parents=True)
    if git_file:  # a linked worktree has a `.git` file, not a directory
        (work / name / ".git").write_text("gitdir: elsewhere\n")
    else:
        (work / name / ".git").mkdir()


def _runtime(name: str | None):
    def detect() -> str:
        if name is None:
            raise SandboxUnavailableError("no runtime")
        return name

    return detect


def test_repos_are_the_git_clones_under_the_work_directory(tmp_path) -> None:
    work = tmp_path / "Work"
    _clone(work, "agentic-coding-monitor")
    _clone(work, "linked", git_file=True)
    (work / "notes").mkdir()  # a plain directory is not a repo
    (work / ".hidden").mkdir()
    (work / "stray.txt").write_text("x")

    profile = detect_worker_profile(
        "m3max", work_dir=work, probe=lambda tool: False, runtime=_runtime(None)
    )

    assert profile.repos == ["agentic-coding-monitor", "linked"]


def test_a_missing_work_directory_means_no_repos(tmp_path) -> None:
    profile = detect_worker_profile(
        "m3max", work_dir=tmp_path / "nope", probe=lambda tool: False, runtime=_runtime(None)
    )
    assert profile.repos == []


def test_harnesses_are_the_registry_entries_whose_cli_answers(tmp_path) -> None:
    seen: list[str] = []

    def probe(tool: str) -> bool:
        seen.append(tool)
        return tool == "claude"

    profile = detect_worker_profile("w", work_dir=tmp_path, probe=probe, runtime=_runtime(None))

    assert profile.harnesses == ["claude"]
    assert "codex" in seen  # asked, and absent


def test_codex_is_advertised_when_its_cli_is_installed(tmp_path) -> None:
    profile = detect_worker_profile(
        "w", work_dir=tmp_path, probe=lambda tool: tool in {"claude", "codex"}, runtime=_runtime(None)
    )
    assert profile.harnesses == ["claude", "codex"]


def test_sandbox_is_the_container_runtime_or_nothing(tmp_path) -> None:
    kwargs = {"work_dir": tmp_path, "probe": lambda tool: False}
    assert detect_worker_profile("w", runtime=_runtime("podman"), **kwargs).sandbox == "podman"
    assert detect_worker_profile("w", runtime=_runtime(None), **kwargs).sandbox is None


def test_host_defaults_to_the_short_hostname_and_can_be_overridden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.queue_worker.socket.gethostname", lambda: "home-lab.local")
    kwargs = {"work_dir": tmp_path, "probe": lambda tool: False, "runtime": _runtime(None)}

    assert detect_worker_profile("w", **kwargs).host == "home-lab"
    assert detect_worker_profile("w", host="xps", **kwargs).host == "xps"


def test_pools_are_free_form_but_must_be_non_blank_and_unique(tmp_path) -> None:
    kwargs = {"work_dir": tmp_path, "probe": lambda tool: False, "runtime": _runtime(None)}

    profile = detect_worker_profile("w", pools=["claude-m3", "codex-shared", "claude-m3"], **kwargs)
    assert profile.pools == ["claude-m3", "codex-shared"]
    with pytest.raises(ValueError):
        detect_worker_profile("w", pools=["claude-m3", " "], **kwargs)


def test_a_blank_worker_name_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        detect_worker_profile("  ", work_dir=tmp_path, probe=lambda t: False, runtime=_runtime(None))


def test_register_with_hands_the_profile_to_the_queue(tmp_path) -> None:
    from sdlc.queue import QueueStore

    work = tmp_path / "Work"
    _clone(work, "agentic-coding-monitor")
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    profile = detect_worker_profile(
        "m3max",
        pools=["claude-m3"],
        host="m3",
        work_dir=work,
        probe=lambda tool: tool == "claude",
        runtime=_runtime("podman"),
    )

    record = profile.register_with(store, slots=2, slots_free=1)

    assert (record.name, record.host, record.pools) == ("m3max", "m3", ["claude-m3"])
    assert record.harnesses == ["claude"] and record.sandbox == "podman"
    assert record.repos == ["agentic-coding-monitor"] and record.slots_free == 1
