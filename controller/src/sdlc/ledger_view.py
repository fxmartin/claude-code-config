# ABOUTME: CLI-facing ledger helpers — DB path resolution + markdown render hook.
# ABOUTME: Story 7.3-001 — bridges run_build to the Epic-04 sdlc-state.sh renderer.

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from sdlc.build import Ledger  # re-exported for the CLI's convenience

__all__ = ["Ledger", "default_db_path", "find_state_script", "make_render_view"]

# The ledger lives at the repo root, matching `sdlc-state.sh`'s default.
_DEFAULT_DB_NAME = ".sdlc-state.db"

# Where the markdown read-model is regenerated (Story 4.2-002 contract).
_PROGRESS_VIEW = "docs/stories/.build-progress.md"


def default_db_path(root: Path | None = None) -> Path:
    """Resolve the ledger path, matching `sdlc-state.sh`'s `.sdlc-state.db`."""
    return (root or Path.cwd()) / _DEFAULT_DB_NAME


def find_state_script(root: Path | None = None) -> Path | None:
    """Locate `scripts/sdlc-state.sh` for the markdown renderer, if present."""
    root = root or Path.cwd()
    candidate = root / "scripts" / "sdlc-state.sh"
    return candidate if candidate.is_file() else None


def make_render_view(
    db_path: Path, root: Path | None = None
) -> Callable[[str], None] | None:
    """Return a callable that regenerates the markdown view from the ledger.

    The renderer reuses the Epic-04 `sdlc-state.sh render` path so the markdown
    read-model stays byte-identical to the resume/summary flows. When the script
    or `bash` is unavailable (e.g. standalone install) this returns ``None`` so
    ``run_build`` simply skips the render — the SQLite ledger remains the source
    of truth either way.
    """
    root = root or Path.cwd()
    script = find_state_script(root)
    if script is None or shutil.which("bash") is None:
        return None

    progress_path = root / _PROGRESS_VIEW

    def _render(_run_id: str) -> None:
        # Best-effort: a render failure must never fail an otherwise-good build.
        if not progress_path.parent.is_dir():
            return
        subprocess.run(
            [
                "bash",
                str(script),
                "--db",
                str(db_path),
                "render",
                "--out",
                str(progress_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    return _render
