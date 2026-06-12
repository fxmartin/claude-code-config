# ABOUTME: Coverage tests for sdlc.__init__ version resolution (Story 7.1-001).
# ABOUTME: Forces the PackageNotFoundError fallback path and __main__ invocation.

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_resolve_version_fallback_to_pyproject(tmp_path: Path) -> None:
    """When the package is not installed, _resolve_version reads pyproject.toml."""
    # Force importlib.metadata.version to raise PackageNotFoundError so the
    # fallback branch (lines 19-25 of __init__.py) is exercised.
    with patch("importlib.metadata.version", side_effect=PackageNotFoundError("sdlc-controller")):
        # Re-import the private helper directly to avoid module caching masking
        # the fallback path.
        import importlib
        import sdlc
        importlib.reload(sdlc)
        fallback_ver = sdlc._resolve_version()  # type: ignore[attr-defined]

    assert fallback_ver == _pyproject_version()


def test_resolve_version_uses_installed_metadata() -> None:
    """When installed metadata is present, _resolve_version returns it directly."""
    from sdlc import _resolve_version  # type: ignore[attr-defined]
    # The package is installed in dev mode — metadata must be available.
    ver = _resolve_version()
    assert ver == _pyproject_version()


def test_main_guard_invokes_app() -> None:
    """Running `python -m sdlc.cli` (the __main__ guard) must not crash."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "sdlc.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    # --version should print the version and exit 0 regardless of invocation mode.
    assert result.returncode == 0
    assert _pyproject_version() in result.stdout


def test_main_guard_branch_via_runpy() -> None:
    """The __main__ guard (cli.py line 93) is exercised in-process via runpy."""
    import runpy
    from unittest.mock import patch

    # Patch typer.Typer.__call__ to prevent the CLI from actually parsing
    # sys.argv during runpy execution, which would cause SystemExit.
    with patch("typer.Typer.__call__", return_value=None):
        runpy.run_module("sdlc.cli", run_name="__main__", alter_sys=False)
