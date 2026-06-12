# ABOUTME: Regression tests guarding that JSON schemas ship inside the wheel.
# ABOUTME: Catches the Epic-07 E2E defect where `sdlc validate` broke under install.

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from sdlc.contracts import AGENT_SCHEMAS, SCHEMA_DIR, load_schema

# The controller package directory (controller/) is two levels up from this
# test file: tests/ -> controller/.
CONTROLLER_DIR = Path(__file__).resolve().parents[1]

# A valid `build`-agent result block. Validating it must exit 0 from any cwd.
_VALID_BUILD_BLOCK = (
    "<<<RESULT_JSON>>>\n"
    + json.dumps(
        {
            "branch_name": "feature/7.2-001",
            "build_status": "SUCCESS",
            "commit_sha": "abc123def456",
        }
    )
    + "\n<<<END_RESULT>>>\n"
)


def test_schemas_are_package_resources() -> None:
    """Every schema is reachable as a resource of the installed `sdlc` package.

    This is the hermetic guard for the class of bug where schemas live outside
    the package and a `parents[N] / "schemas"` path silently breaks once the
    source tree is gone (e.g. under `uv tool install`).
    """
    package_files = resources.files("sdlc")
    for filename in AGENT_SCHEMAS.values():
        resource = package_files / "schemas" / filename
        assert resource.is_file(), f"schema not bundled in package: {filename}"
        # Must be parseable JSON loadable through the public API too.
        assert load_schema(
            next(k for k, v in AGENT_SCHEMAS.items() if v == filename)
        )


def test_schema_dir_resolves_inside_the_package() -> None:
    """SCHEMA_DIR points inside the `sdlc` package, not the source-tree sibling."""
    # The resolved schema dir must live under the imported package's directory.
    package_root = Path(str(resources.files("sdlc")))
    schema_root = Path(str(SCHEMA_DIR))
    assert schema_root == package_root / "schemas"


@pytest.mark.slow
def test_installed_wheel_validates_from_arbitrary_cwd(tmp_path: Path) -> None:
    """Build the wheel into a throwaway venv, then run `sdlc validate build`.

    Reproduces the real-world install path: with the source tree absent, the
    console script must still find its schemas and exit 0 on a valid block and
    non-zero on an invalid one. Skips gracefully when offline build tooling
    (pip/build) is unavailable.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build the wheel")

    # 1. Build the wheel from the controller source into tmp_path.
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist_dir), str(CONTROLLER_DIR)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment:\n{build.stderr}")
    wheels = list(dist_dir.glob("*.whl"))
    assert wheels, "no wheel produced"

    # 2. Create an isolated venv and install ONLY the built wheel into it.
    #    Use `uv venv`/`uv pip` rather than stdlib venv: under uv-managed
    #    interpreters `venv --with-pip` aborts in ensurepip.
    env_dir = tmp_path / "venv"
    venv_build = subprocess.run(
        ["uv", "venv", str(env_dir)],
        capture_output=True,
        text=True,
    )
    if venv_build.returncode != 0:
        pytest.skip(f"cannot create venv:\n{venv_build.stderr}")
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(bin_dir / "python"), str(wheels[0])],
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        pytest.skip(f"offline: cannot install wheel deps:\n{install.stderr}")

    sdlc_bin = bin_dir / ("sdlc.exe" if os.name == "nt" else "sdlc")
    assert sdlc_bin.exists(), "sdlc console script not installed"

    # 3. Run `sdlc validate build` from a cwd far from the source tree.
    work = tmp_path / "elsewhere"
    work.mkdir()

    ok = subprocess.run(
        [str(sdlc_bin), "validate", "build"],
        input=_VALID_BUILD_BLOCK,
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, (
        f"valid block should validate from an installed wheel; "
        f"stdout={ok.stdout!r} stderr={ok.stderr!r}"
    )

    bad = subprocess.run(
        [str(sdlc_bin), "validate", "build"],
        input="<<<RESULT_JSON>>>\n{}\n<<<END_RESULT>>>\n",
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0, "invalid block must exit non-zero"
