# ABOUTME: Issue #944 — a controller reinstalled under a direct run parks its stories.
# ABOUTME: Install marker, eager imports, and the build/resume/fix park paths.

from __future__ import annotations

import os
import pkgutil
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

import sdlc
from sdlc import build as build_mod
from sdlc import install_guard
from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.fix_issue import FixOptions, run_fix
from sdlc.registry import Registry
from sdlc import resume as resume_mod
from sdlc.resume import run_resume

from test_build import FakeDispatcher, _story
from test_fix_issue import FakeGh, RecordingDispatcher, _issue_json
from test_resume import _make_project, _seed_interrupted

HINT = "controller reinstalled mid-run — run `sdlc resume`"


class _FakeInstall:
    """The install marker the guard reads, moved by a test to fake a reinstall."""

    def __init__(self, monkeypatch) -> None:
        self.marker: tuple[str, int] | None = ("2.0.0", 1)
        monkeypatch.setattr(install_guard, "install_marker", lambda: self.marker)
        monkeypatch.setattr(install_guard, "_armed", None)

    swap_to = ("2.0.1", 2)

    def swap(self) -> None:
        self.marker = self.swap_to


@pytest.fixture
def install(monkeypatch) -> _FakeInstall:
    return _FakeInstall(monkeypatch)


class _ReinstallAfter(FakeDispatcher):
    """Swaps the install once ``agent_type`` has been dispatched."""

    def __init__(self, install: _FakeInstall, agent_type: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._install = install
        self._agent_type = agent_type

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        result = super().__call__(agent_type, prompt, story=story, **kwargs)
        if agent_type == self._agent_type:
            self._install.swap()
        return result


def _events(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT message FROM events")]


def _opts(**kwargs) -> BuildOptions:
    return BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, **kwargs)


# --- the marker ---------------------------------------------------------------


class _Dist:
    version = "2.0.0"

    def __init__(self, metadata: Path) -> None:
        self._metadata = metadata

    @property
    def files(self):
        metadata = self._metadata

        class _File:
            name = "METADATA"

            @staticmethod
            def locate() -> Path:
                return metadata

        return [_File()]


def test_install_marker_moves_when_the_dist_info_is_recreated(tmp_path, monkeypatch) -> None:
    # uv hardlinks wheel files from its cache, so METADATA keeps its old mtime
    # across a reinstall: only the directory it lands in is new.
    metadata = tmp_path / "sdlc_controller-2.0.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: sdlc-controller\n")
    monkeypatch.setattr(install_guard, "distribution", lambda _name: _Dist(metadata))

    first = install_guard.install_marker()
    assert first is not None and first[0] == "2.0.0"

    os.utime(metadata.parent, ns=(1, 1))
    assert install_guard.install_marker() != first


@pytest.mark.parametrize("failure", [
    PackageNotFoundError("sdlc-controller"),  # the venv is gone
    TypeError("METADATA has no text yet"),  # the dist-info is half written
])
def test_install_marker_is_none_when_the_install_is_unreadable(monkeypatch, failure) -> None:
    def unreadable(_name):
        raise failure

    monkeypatch.setattr(install_guard, "distribution", unreadable)
    assert install_guard.install_marker() is None


def test_install_marker_is_none_with_no_metadata_recorded(monkeypatch, tmp_path) -> None:
    dist = _Dist(tmp_path / "METADATA")
    monkeypatch.setattr(_Dist, "files", None)
    monkeypatch.setattr(install_guard, "distribution", lambda _name: dist)
    assert install_guard.install_marker() is None


def test_install_marker_reads_this_environment() -> None:
    marker = install_guard.install_marker()
    assert marker is None or marker[0] == sdlc.__version__


def test_reinstalled_is_false_until_the_marker_moves(install) -> None:
    assert install_guard.reinstalled() is False  # not armed
    install_guard.arm()
    assert install_guard.reinstalled() is False
    install.swap()
    assert install_guard.reinstalled() is True


def test_a_marker_that_cannot_be_read_is_not_a_reinstall(install) -> None:
    # Mid-swap the dist is missing: unknown, and the next boundary sees the new one.
    install_guard.arm()
    install.marker = None
    assert install_guard.reinstalled() is False


def test_arm_keeps_the_first_snapshot(install) -> None:
    # `sdlc fix 1 2` opens several runs in one process, all on the code it imported.
    install_guard.arm()
    install.swap()
    install_guard.arm()
    assert install_guard.reinstalled() is True


# --- eager imports ------------------------------------------------------------


def test_arm_imports_every_controller_module(install, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "sdlc.parsers", raising=False)
    install_guard.arm()
    names = [m.name for m in pkgutil.walk_packages(sdlc.__path__, "sdlc.")]
    assert "sdlc.parsers" in names
    assert [name for name in names if name not in sys.modules] == []


def test_a_module_that_will_not_import_does_not_stop_the_run(install, monkeypatch) -> None:
    def broken(_name):
        raise ImportError("optional dependency missing")

    monkeypatch.setattr(install_guard.importlib, "import_module", broken)
    install_guard.arm()
    assert install_guard.reinstalled() is False


# --- what counts as a swap crash ----------------------------------------------


def test_a_missing_controller_module_is_a_reinstall_crash(install) -> None:
    install_guard.arm()
    exc = ModuleNotFoundError("No module named 'sdlc.parsers'", name="sdlc.parsers")
    assert install_guard.is_reinstall_crash(exc) is True


def test_a_missing_third_party_module_is_a_bug_on_a_stable_install(install) -> None:
    install_guard.arm()
    exc = ModuleNotFoundError("No module named 'yaml'", name="yaml")
    assert install_guard.is_reinstall_crash(exc) is False


def test_a_missing_third_party_module_mid_swap_is_a_reinstall_crash(install) -> None:
    install_guard.arm()
    install.marker = None
    exc = ModuleNotFoundError("No module named 'yaml'", name="yaml")
    assert install_guard.is_reinstall_crash(exc) is True


def test_an_unrelated_error_is_not_a_reinstall_crash(install) -> None:
    install_guard.arm()
    assert install_guard.is_reinstall_crash(ValueError("sdlc.parsers")) is False


# --- run_build ----------------------------------------------------------------


@pytest.mark.parametrize("sequential", [True, False])
def test_a_marker_change_mid_story_parks_it_with_the_resume_hint(
    tmp_path, install, sequential
) -> None:
    db = tmp_path / "l.db"
    disp = _ReinstallAfter(install, "build")

    result = run_build(
        _opts(sequential=sequential), queue=[_story("99.1-001")],
        ledger=Ledger(db), dispatcher=disp, preflight=lambda: True,
    )

    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    assert HINT in _events(db)
    # The stage after the swap never started on the mixed install.
    assert [agent for agent, _ in disp.calls].count("build") == 1
    assert "review" not in [agent for agent, _ in disp.calls]


def test_pending_stories_park_without_a_dispatch(tmp_path, install) -> None:
    db = tmp_path / "l.db"
    disp = _ReinstallAfter(install, "merge")

    result = run_build(
        _opts(sequential=True), queue=[_story("99.1-001"), _story("99.1-002")],
        ledger=Ledger(db), dispatcher=disp, preflight=lambda: True,
    )

    assert result.story_status == {"99.1-001": "DONE", "99.1-002": "NEEDS_ATTENTION"}
    assert all(story != "99.1-002" for _, story in disp.calls)


def test_a_stable_install_builds_to_done(tmp_path, install) -> None:
    result = run_build(
        _opts(sequential=True), queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"), dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"


@pytest.mark.parametrize("sequential", [True, False])
def test_an_import_error_from_the_swap_parks_instead_of_failing(
    tmp_path, install, monkeypatch, sequential
) -> None:
    """The field failure: `No module named 'sdlc.parsers'` in a worker thread."""

    def crash(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'sdlc.parsers'", name="sdlc.parsers")

    monkeypatch.setattr(build_mod, "_run_story", crash)
    db = tmp_path / "l.db"

    result = run_build(
        _opts(sequential=sequential), queue=[_story("99.1-001")],
        ledger=Ledger(db), dispatcher=FakeDispatcher(), preflight=lambda: True,
    )

    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    assert HINT in _events(db)
    assert not [e for e in _events(db) if "No module named" in e and "raised" in e]


def test_an_unrelated_import_error_still_fails_the_story(
    tmp_path, install, monkeypatch
) -> None:
    def crash(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    monkeypatch.setattr(build_mod, "_run_story", crash)

    result = run_build(
        _opts(sequential=False), queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"), dispatcher=FakeDispatcher(), preflight=lambda: True,
    )

    assert result.story_status["99.1-001"] == "FAILED"


# --- run_resume ---------------------------------------------------------------


@pytest.mark.parametrize("concurrency", [1, 2])
def test_a_marker_change_during_resume_parks_again(
    tmp_path, install, monkeypatch, concurrency
) -> None:
    root = _make_project(tmp_path)
    monkeypatch.chdir(root)
    db = root / ".sdlc-state.db"
    _seed_interrupted(db)
    install.swap_to = ("2.0.2", 3)
    dispatch = _ReinstallAfter(install, "review")

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=dispatch, root=root,
        concurrency=concurrency, registry=Registry(tmp_path / "registry.json"),
    )

    assert "NEEDS_ATTENTION" in result.story_status.values()
    assert "FAILED" not in result.story_status.values()
    assert HINT in _events(db)
    assert "merge" not in [agent for agent, _ in dispatch.calls]


def test_an_import_error_from_the_swap_parks_a_resumed_story(
    tmp_path, install, monkeypatch
) -> None:
    def crash(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'sdlc.parsers'", name="sdlc.parsers")

    root = _make_project(tmp_path)
    monkeypatch.chdir(root)
    db = root / ".sdlc-state.db"
    _seed_interrupted(db)
    monkeypatch.setattr(resume_mod, "_run_story_rate_limited", crash)

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=root,
        concurrency=1, registry=Registry(tmp_path / "registry.json"),
    )

    assert "FAILED" not in result.story_status.values()
    assert HINT in _events(db)


# --- run_fix ------------------------------------------------------------------


def _fix(tmp_path, dispatch) -> tuple:
    db = tmp_path / "fix.db"
    result = run_fix(
        FixOptions(issue=1), ledger=Ledger(db), dispatcher=dispatch,
        preflight=lambda: True, runner=FakeGh(_issue_json()), root=tmp_path,
    )
    return result, db


def test_a_marker_change_mid_fix_parks_with_the_resume_hint(tmp_path, install) -> None:
    dispatch = RecordingDispatcher()
    inner = dispatch.__call__

    class _Swapping:
        def __call__(self, agent_type, *args, **kwargs):
            result = inner(agent_type, *args, **kwargs)
            if agent_type == "build":
                install.swap()
            return result

        def __getattr__(self, name):
            return getattr(dispatch, name)

    result, db = _fix(tmp_path, _Swapping())

    assert result.status == "NEEDS_ATTENTION"
    assert HINT in _events(db)
    assert "merge" not in dispatch.agents()


def test_an_import_error_from_the_swap_parks_a_fix(tmp_path, install, monkeypatch) -> None:
    from sdlc import fix_issue

    def crash(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'sdlc.parsers'", name="sdlc.parsers")

    monkeypatch.setattr(fix_issue, "_fix_change_class", crash)

    result, db = _fix(tmp_path, RecordingDispatcher())

    assert result.status == "NEEDS_ATTENTION"
    assert HINT in _events(db)
