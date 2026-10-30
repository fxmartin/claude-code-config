# ABOUTME: Issue #944 — notice `uv tool install --force` replacing the running controller.
# ABOUTME: Eager imports at run start, an install marker, and the park-not-crash test.

from __future__ import annotations

import importlib
import pkgutil
from importlib.metadata import distribution
from pathlib import Path

import sdlc

__all__ = [
    "arm",
    "install_marker",
    "is_reinstall_crash",
    "park_message",
    "reinstalled",
]

# The marker this process started on; None until armed, or with no readable install.
_armed: tuple[str, int] | None = None


def park_message(run_id: str) -> str:
    """The event every parked story carries, naming the command that re-enters it.

    It names the run because a parked run closes ``NEEDS_ATTENTION``, which a bare
    `sdlc resume` deliberately never picks (:meth:`Ledger.latest_resumable_run`).
    """
    return f"controller reinstalled mid-run — run `sdlc resume --run {run_id}`"


def install_marker() -> tuple[str, int] | None:
    """``(version, dist-info mtime)`` of the installed controller, or None.

    The directory's mtime, not METADATA's: uv hardlinks wheel files from its
    cache, so they keep their old mtime when the same version is reinstalled.
    None is unknown, not a reinstall: a read landing mid-swap can meet a missing
    or half-written dist, whose failures are not all of one type, and it runs
    between stages of a live run.
    """
    try:
        dist = distribution("sdlc-controller")
        metadata = next(f for f in dist.files or () if f.name == "METADATA")
        return dist.version, Path(metadata.locate()).parent.stat().st_mtime_ns
    except Exception:  # noqa: BLE001 - see the docstring: unknown, not an error
        return None


def _import_all() -> None:
    """Load every controller module, so a later swap cannot break a lazy import.

    A module that will not import is left for the code path that needs it to
    report — most runs never reach it, and none should stop here.
    """
    for module in pkgutil.walk_packages(sdlc.__path__, "sdlc."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            continue


def arm() -> None:
    """Snapshot the install, then import the rest of the controller from it.

    In that order, so a swap landing during the imports still reads as a
    reinstall. The first snapshot stands: a process keeps the code it imported,
    however many runs (`sdlc fix 1 2`) it goes on to open.
    """
    global _armed
    if _armed is None:
        _armed = install_marker()
    _import_all()


def reinstalled() -> bool:
    """True when the install on disk is no longer the one this process armed on."""
    current = install_marker()
    return _armed is not None and current is not None and current != _armed


def is_reinstall_crash(exc: BaseException) -> bool:
    """True when ``exc`` is an import the install swap broke, not a bug in a story.

    A missing `sdlc.*` module has no other cause; any other module counts only
    while the install has visibly moved (or is mid-swap and unreadable).
    """
    if not isinstance(exc, ImportError):
        return False
    # Only a *missing* module: "cannot import name" from a present `sdlc.*`
    # module is a code bug unless the marker below shows the install moved.
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".", 1)[0] == "sdlc":
        return True
    return install_marker() != _armed
