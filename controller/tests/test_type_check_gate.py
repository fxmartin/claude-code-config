# ABOUTME: Tests that the mypy ratchet is actually wired into the gate (Issue #586).
# ABOUTME: Asserts the pyproject config, strict allowlist, CI job, local script, and docs contract.

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from sdlc.type_check import load_baseline

_CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CONTROLLER_ROOT.parent
_PYPROJECT = _CONTROLLER_ROOT / "pyproject.toml"
_BASELINE = _CONTROLLER_ROOT / ".mypy-baseline.json"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = _REPO_ROOT / "scripts" / "run-type-check.sh"
_DOC = _REPO_ROOT / "docs" / "python-best-practices.md"


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _mypy_config() -> dict:
    return _pyproject()["tool"]["mypy"]


# ---------------------------------------------------------------------------
# Checker configuration (rung 0: the repo-wide floor)
# ---------------------------------------------------------------------------


def test_pyproject_configures_mypy() -> None:
    assert "mypy" in _pyproject()["tool"], "controller/pyproject.toml declares no checker"


def test_mypy_checks_the_controller_source_tree() -> None:
    config = _mypy_config()
    assert config["files"] == ["src"]
    assert config["python_version"] == "3.11"


def test_repo_wide_floor_enables_the_real_bug_checks() -> None:
    # Rung 0 stays permissive about *annotation coverage* but never about
    # checks that catch genuine defects — that is the whole point of the gate.
    config = _mypy_config()
    for flag in (
        "check_untyped_defs",
        "warn_redundant_casts",
        "warn_unused_ignores",
        "warn_unused_configs",
        "strict_equality",
        "extra_checks",
    ):
        assert config.get(flag) is True, f"rung 0 must enable {flag}"


def test_repo_wide_floor_does_not_silently_skip_missing_imports() -> None:
    # A blanket ignore_missing_imports turns whole dependencies into Any and
    # hollows out the gate; stubs are pinned as dev deps instead.
    assert _mypy_config().get("ignore_missing_imports") is not True


def test_type_stubs_are_pinned_as_dev_dependencies() -> None:
    dev = " ".join(_pyproject()["project"]["optional-dependencies"]["dev"])
    assert "mypy" in dev, "mypy must be installed by `uv sync --extra dev`"
    for stub in ("types-PyYAML", "types-jsonschema"):
        assert stub in dev, f"{stub} must be pinned, not fetched at gate time"


# ---------------------------------------------------------------------------
# Strict allowlist (rung 2: the growing head of the ratchet)
# ---------------------------------------------------------------------------


# The per-module flags `strict = true` stands for. mypy treats `strict` itself
# as global-only — setting it inside an override silently promotes the entire
# tree to rung 1 — so the allowlist must spell the flags out.
_STRICT_FLAGS = (
    "disallow_any_generics",
    "disallow_subclassing_any",
    "disallow_untyped_calls",
    "disallow_untyped_defs",
    "disallow_incomplete_defs",
    "disallow_untyped_decorators",
    "check_untyped_defs",
    "warn_return_any",
    "warn_unused_ignores",
    "no_implicit_reexport",
    "strict_equality",
    "extra_checks",
)


def _strict_overrides() -> list[dict]:
    return [
        override
        for override in _pyproject()["tool"]["mypy"].get("overrides", [])
        if override.get("disallow_untyped_defs") is True
    ]


def test_a_strict_allowlist_exists_and_is_non_empty() -> None:
    modules = [m for override in _strict_overrides() for m in override["module"]]
    assert modules, "the ratchet needs a strict allowlist to grow"


def test_strict_allowlist_spells_out_every_strict_flag() -> None:
    for override in _strict_overrides():
        for flag in _STRICT_FLAGS:
            assert override.get(flag) is True, f"allowlist is missing {flag}"


def test_no_override_sets_the_global_only_strict_flag() -> None:
    # Guard the exact trap this config fell into: `strict = true` in an
    # override applies globally, silently red-lining all 63 modules.
    for override in _pyproject()["tool"]["mypy"].get("overrides", []):
        assert "strict" not in override, (
            "`strict` is global-only; expand it into per-module flags instead"
        )


def test_strict_allowlist_modules_are_real() -> None:
    for override in _strict_overrides():
        for module in override["module"]:
            relative = module.removeprefix("sdlc.").replace(".", "/")
            assert (
                _CONTROLLER_ROOT / "src" / "sdlc" / f"{relative}.py"
            ).is_file(), f"allowlisted module does not exist: {module}"


def test_the_ratchet_module_itself_is_strict() -> None:
    # The gate's own logic must live at the top rung, not the floor.
    modules = [m for override in _strict_overrides() for m in override["module"]]
    assert "sdlc.type_check" in modules


# ---------------------------------------------------------------------------
# The committed baseline
# ---------------------------------------------------------------------------


def test_baseline_file_is_committed_and_loadable() -> None:
    assert _BASELINE.is_file(), "the ratchet needs a checked-in baseline"
    load_baseline(_BASELINE)  # raises TypeCheckBaselineError if malformed


def test_baseline_signatures_point_at_files_that_still_exist() -> None:
    for signature in load_baseline(_BASELINE).violations:
        path = signature.split("|", 1)[0]
        assert (_CONTROLLER_ROOT / path).is_file(), f"stale baseline entry: {signature}"


def test_no_baselined_violation_sits_in_a_strict_allowlisted_module() -> None:
    # A module is only promoted to the allowlist once it is clean; otherwise the
    # ratchet would be handing out permanent exemptions at the top rung.
    allowlisted = {
        m for override in _strict_overrides() for m in override["module"]
    }
    for signature in load_baseline(_BASELINE).violations:
        path = signature.split("|", 1)[0]
        module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
        assert module not in allowlisted, f"{module} is allowlisted but still dirty"


# ---------------------------------------------------------------------------
# Gate wiring: local entry point + CI job
# ---------------------------------------------------------------------------


def test_local_entry_point_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file(), "no local type-check target"
    assert _SCRIPT.stat().st_mode & 0o111, f"{_SCRIPT} must be executable"


def test_ci_runs_the_type_check_gate() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    runs = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert any(
        "run-type-check.sh" in run for run in runs
    ), "no CI job invokes the type-check gate"


def test_ci_type_check_job_installs_the_dev_extra() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = next(
        job
        for job in workflow["jobs"].values()
        if any("run-type-check.sh" in step.get("run", "") for step in job.get("steps", []))
    )
    steps = " ".join(str(step) for step in job["steps"])
    assert "astral-sh/setup-uv" in steps, "the gate runs mypy through uv"


# ---------------------------------------------------------------------------
# The documented contract
# ---------------------------------------------------------------------------


def test_docs_describe_the_strictness_ladder() -> None:
    doc = _DOC.read_text(encoding="utf-8")
    assert "strictness ladder" in doc.lower()
    assert "run-type-check.sh" in doc


def test_docs_no_longer_claim_an_unratcheted_type_gate() -> None:
    # The old row promised "uv run mypy . / blocks on: any error", which is not
    # the contract a ratcheted rollout offers.
    doc = _DOC.read_text(encoding="utf-8")
    assert "| Type check | `uv run mypy .` | Any error |" not in doc
