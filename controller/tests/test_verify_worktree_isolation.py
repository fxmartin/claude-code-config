# ABOUTME: Hermetic tests for scripts/verify-worktree-isolation.sh (Epic-29 evidence tool).
# ABOUTME: Drives the script with fake adapters so its isolation logic is proven without a model CLI.

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-worktree-isolation.sh"

# A fake adapter honours the script's env-mirror contract (VERIFY_MARKER_FILE /
# VERIFY_MARKER_TOKEN) so isolation can be exercised with zero model calls.
_GOOD_ADAPTER = """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--version" ]] && { echo "fake 1.0.0"; exit 0; }
cat >/dev/null
printf '%s\\n' "${VERIFY_MARKER_TOKEN:?}" > "${VERIFY_MARKER_FILE:?}"
echo "<<<RESULT_JSON>>>"; echo '{"build_status":"SUCCESS"}'; echo "<<<END_RESULT>>>"
"""

# Deliberately breaks isolation by writing into sibling worktrees via ../.
_LEAKY_ADAPTER = """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--version" ]] && { echo "leaky 0.1"; exit 0; }
cat >/dev/null
printf '%s\\n' "${VERIFY_MARKER_TOKEN:?}" > "${VERIFY_MARKER_FILE:?}"
for d in ../wt-*; do [[ -d "$d" ]] && echo leak > "$d/${VERIFY_MARKER_FILE}" || true; done
"""

# Reports success without ever writing the marker — the "did not write
# unattended" failure mode (a verified-negative outcome the story allows).
_NOOP_ADAPTER = """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--version" ]] && { echo "noop 0.0"; exit 0; }
cat >/dev/null
echo "<<<RESULT_JSON>>>"; echo '{"build_status":"SUCCESS"}'; echo "<<<END_RESULT>>>"
"""


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="verify-worktree-isolation.sh needs bash + git",
)


def _adapter(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "adapter.sh"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run(adapter: Path) -> subprocess.CompletedProcess[str]:
    # HOME points at a scratch dir so the run never reads the developer's git
    # config; the script sets per-commit identity anyway, but this keeps it hermetic.
    env = {**os.environ, "HOME": str(adapter.parent)}
    return subprocess.run(
        ["bash", str(SCRIPT), "--adapter", str(adapter), "--label", "faketest"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_isolated_adapter_verifies_clean(tmp_path: Path) -> None:
    result = _run(_adapter(tmp_path, _GOOD_ADAPTER))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "no cross-contamination" in result.stdout
    assert "shared repo root untouched" in result.stdout


def test_cross_contaminating_adapter_is_caught(tmp_path: Path) -> None:
    result = _run(_adapter(tmp_path, _LEAKY_ADAPTER))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "RESULT: FAIL" in result.stdout
    assert "leaked into this worktree" in result.stdout


def test_adapter_that_never_writes_is_verified_negative(tmp_path: Path) -> None:
    result = _run(_adapter(tmp_path, _NOOP_ADAPTER))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "own marker MISSING" in result.stdout


def test_missing_adapter_is_a_usage_error(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--adapter", str(tmp_path / "nope.sh")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "not executable" in result.stderr


def test_unknown_argument_is_a_usage_error() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "unexpected argument" in result.stderr
