# ABOUTME: Tests for ledger view helpers + preflight detection (Story 7.3-001).
# ABOUTME: Render hook is exercised against a fake bash; no real renderer runs.

from __future__ import annotations

from pathlib import Path

from sdlc.build import detect_test_command, default_preflight
from sdlc.ledger_view import (
    default_db_path,
    find_state_script,
    make_render_view,
)


def test_default_db_path_matches_state_script_default(tmp_path) -> None:
    assert default_db_path(tmp_path) == tmp_path / ".sdlc-state.db"


def test_find_state_script_present(tmp_path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "sdlc-state.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert find_state_script(tmp_path) == script


def test_find_state_script_absent(tmp_path) -> None:
    assert find_state_script(tmp_path) is None


def test_make_render_view_none_without_script(tmp_path) -> None:
    """No sdlc-state.sh → no render hook (the build still runs)."""
    assert make_render_view(tmp_path / ".sdlc-state.db", tmp_path) is None


def test_make_render_view_invokes_script(tmp_path, monkeypatch) -> None:
    """When the script exists, the hook shells out to it (subprocess mocked)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "sdlc-state.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "docs" / "stories").mkdir(parents=True)

    calls = {}
    import sdlc.ledger_view as lv

    monkeypatch.setattr(lv.shutil, "which", lambda _: "/bin/bash")

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd

        class _C:
            returncode = 0

        return _C()

    monkeypatch.setattr(lv.subprocess, "run", fake_run)
    hook = make_render_view(tmp_path / ".sdlc-state.db", tmp_path)
    assert hook is not None
    hook("run-123")
    assert "render" in calls["cmd"]


# ---------------------------------------------------------------------------
# Preflight detection
# ---------------------------------------------------------------------------

def test_detect_test_command_python(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["uv", "run", "pytest"]


def test_detect_test_command_npm(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["npm", "test"]


def test_detect_test_command_makefile(tmp_path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\techo ok\n", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["make", "test"]


def test_detect_test_command_bats(tmp_path) -> None:
    (tmp_path / "test").mkdir()
    assert detect_test_command(tmp_path) == ["bats", "test/"]


def test_detect_test_command_none(tmp_path) -> None:
    assert detect_test_command(tmp_path) is None


def test_default_preflight_passes_when_no_suite(tmp_path) -> None:
    """No detectable test command → preflight is a pass, not a block."""
    assert default_preflight(tmp_path) is True


def test_default_preflight_runs_detected_command(tmp_path, monkeypatch) -> None:
    """The detected command is run and its return code gates the result."""
    (tmp_path / "Makefile").write_text("test:\n\techo ok\n", encoding="utf-8")
    import sdlc.build as build_mod

    class _C:
        returncode = 0

    monkeypatch.setattr(build_mod.subprocess, "run", lambda *a, **k: _C())
    assert default_preflight(tmp_path) is True
