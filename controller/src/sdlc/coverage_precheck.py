# ABOUTME: Deterministic coverage pre-check (Story 27.3-001).
# ABOUTME: Runs the repo's own test+coverage command so a green branch skips the coverage-agent dispatch.

"""Deterministic coverage pre-check for the build pipeline.

The coverage agent is the second-most-expensive dispatch of a story (~18% of
story cost) and is frequently dispatched onto a branch whose build agent
already wrote a complete test suite. This module lets the controller measure
that itself: run the project's own test + coverage command in the story
worktree, compute the coverage of the *changed files only* (the same scope the
coverage gate enforces — never repo-wide coverage), and report the result.

Conservative throughout, mirroring ``change_class.py``: anything the
controller cannot measure deterministically — no instrumentable test command,
a wedged/timed-out run, an unreadable report — is *inconclusive* (``None``),
and the caller dispatches the coverage agent exactly as before 27.3-001. The
pre-check can therefore only ever save a dispatch, never weaken the gate: the
90% criterion itself is unchanged and enforced on real measured numbers.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sdlc import change_class

# Bounds the whole suite run, matching the preflight default (--preflight-timeout).
DEFAULT_TIMEOUT_S = 600

# The ledger skip_reason recorded when a green pre-check skips the dispatch.
SKIP_REASON = "coverage-pre-check"


@dataclass(frozen=True)
class PrecheckResult:
    """The measured outcome of one deterministic pre-check run.

    ``coverage_pct`` is the statement coverage aggregated over the branch's
    changed files that appear in the coverage report; ``None`` when nothing
    measurable changed (docs/tests-only diffs) or the report was unreadable —
    which can never pass the gate, only inform the dispatched agent.
    """

    tests_passed: bool
    coverage_pct: float | None
    command: str


def resolve_coverage_command(root: Path, json_report: Path) -> list[str] | None:
    """The instrumented per-repo coverage command, or ``None`` when unmeasurable.

    Reuses the same per-repo resolution the preflight (and the coverage
    prompt's framework detection) relies on — :func:`sdlc.build.detect_test_command`
    — rather than inventing a parallel config. Only a pytest-based command with
    ``pytest-cov`` available can be instrumented for a machine-readable report;
    a repo whose gate is a shell script / npm / make target is inconclusive
    (the agent dispatches as today).
    """
    # Local import: build.py imports this module at top level, so the reverse
    # edge must stay off the module import path (same convention as the
    # issue_host adapter imports in build.py).
    from sdlc.build import detect_test_command

    base = detect_test_command(root)
    if not base or "pytest" not in base:
        return None
    if not _dep_present(root, "pytest-cov"):
        return None
    return [*base, "--cov=.", f"--cov-report=json:{json_report}"]


def _dep_present(root: Path, name: str) -> bool:
    """True when ``name`` appears in the project's deps/lock (mirrors build.py)."""
    for fname in ("pyproject.toml", "uv.lock", "requirements.txt"):
        path = root / fname
        if path.is_file() and name in path.read_text(encoding="utf-8"):
            return True
    return False


def changed_files_coverage(report: dict, files: list[str]) -> float | None:
    """Statement coverage aggregated over the changed files in ``report``.

    Changed files absent from the report (docs, test modules, deleted files)
    simply don't count; when *no* changed file was measured the result is
    ``None`` — never skip on absence of evidence.
    """
    measured = report.get("files", {}) if isinstance(report, dict) else {}
    total = covered = 0
    for path in files:
        summary = measured.get(path, {}).get("summary") or {}
        statements = summary.get("num_statements", 0)
        if not isinstance(statements, (int, float)) or statements <= 0:
            continue
        total += statements
        covered += summary.get("covered_lines", 0)
    if total == 0:
        return None
    return 100.0 * covered / total


def run_precheck(
    root: Path,
    base_ref: str,
    branch: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
    runner=subprocess.run,
) -> PrecheckResult | None:
    """Run the project's test + coverage command in ``root`` and measure it.

    Returns ``None`` — pre-check inconclusive, dispatch as today — when no
    instrumentable command resolves or the run itself cannot complete
    (timeout/OS error). Otherwise reports whether the suite was green and the
    changed-file coverage vs ``base_ref...branch``. ``runner`` is the
    subprocess seam tests inject.
    """
    from sdlc.build import IN_TEST_ENV_VAR

    with tempfile.TemporaryDirectory(prefix="sdlc-precheck-") as tmp:
        json_report = Path(tmp) / "coverage.json"
        cmd = resolve_coverage_command(root, json_report)
        if cmd is None:
            return None
        try:
            # The recursion-guard sentinel keeps a project test that invokes
            # the controller's own verbs from recursing into orchestration
            # (Story 12.1-002), exactly as the preflight run exports it.
            completed = runner(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, IN_TEST_ENV_VAR: "1"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        coverage_pct: float | None = None
        try:
            report = json.loads(json_report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
        if report is not None:
            files = change_class.changed_files(root, base_ref, branch)
            coverage_pct = changed_files_coverage(report, files)
        return PrecheckResult(
            tests_passed=completed.returncode == 0,
            coverage_pct=coverage_pct,
            command=" ".join(cmd),
        )


def passes(result: PrecheckResult, threshold: int) -> bool:
    """True only when the suite is green AND measured coverage >= threshold.

    An unmeasured coverage (``None``) can never pass — the gate criterion (90%
    by default) is unchanged and only ever enforced on real numbers.
    """
    return (
        result.tests_passed
        and result.coverage_pct is not None
        and result.coverage_pct >= threshold
    )
