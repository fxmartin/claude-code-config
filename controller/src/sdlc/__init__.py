# ABOUTME: sdlc controller package marker; exposes the resolved version.
# ABOUTME: Version is read from installed metadata, falling back to pyproject.

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version


def _resolve_version() -> str:
    """Return the package version.

    When installed (e.g. via ``uv tool install .``) the version comes from the
    distribution metadata. When running from a source checkout that is not
    installed, fall back to parsing ``pyproject.toml`` so ``sdlc --version``
    still matches the declared release.
    """
    try:
        return _pkg_version("sdlc-controller")
    except PackageNotFoundError:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])


__version__ = _resolve_version()
