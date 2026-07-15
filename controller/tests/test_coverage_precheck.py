# ABOUTME: Tests the deterministic coverage pre-check + controller-owned PR creation (Story 27.3-001).
# ABOUTME: A green pre-check skips the coverage agent; the controller pushes and opens the PR itself.

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import sdlc.build as build_mod
import sdlc.change_class as change_class_mod
import sdlc.coverage_precheck as precheck_mod
from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.cohort import Story
from sdlc.coverage_precheck import (
    PrecheckResult,
    changed_files_coverage,
    passes,
    resolve_coverage_command,
    run_precheck,
)
from sdlc.dispatch import AgentResult

# The coverage payload deliberately omits pr_number/pr_url: with controller-owned
# PR creation the agent no longer opens the change request (AC3).
_PAYLOADS = {
    "build": {"branch_name": "feature/27.3-001", "build_status": "SUCCESS", "commit_sha": "a"},
    "coverage": {"coverage_pct": 95.0, "tests_added": 1, "coverage_status": "PASS"},
    "review": {"pr_number": 300, "approval_status": "APPROVED", "change_count": 0,
               "final_status": "APPROVED"},
    "merge": {"pr_number": 300, "merge_status": "MERGED", "merge_sha": "b",
              "merged_at": "2026-07-15T00:00:00Z"},
}


class _RecordingDispatcher:
    """Records each dispatch's (stage, prompt) and returns a canned success."""

    def __init__(self, overrides=None) -> None:
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.overrides = overrides or {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append(agent_type)
        self.prompts[agent_type] = prompt
        payload = self.overrides.get(agent_type, _PAYLOADS[agent_type])
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def _story() -> Story:
    return Story(
        id="27.3-001", title="t", epic_id="epic-27", epic_name="e",
        epic_file="f.md", priority="Must", points=5, agent_type="python",
    )


class _CrOpener:
    """Stubs _open_story_cr with a scripted sequence of return values."""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, story, ledger, run_id, workdir, base_ref, close_link,
                 cr_terms, *, body, context):
        self.calls.append(context)
        return self.results.pop(0) if self.results else None


def _run(tmp_path, monkeypatch, *, precheck, cr_results=(300,), overrides=None):
    """Drive one code story through run_build with a stubbed pre-check + CR opener."""
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda root, base, branch: ["src/x.py"]
    )
    monkeypatch.setattr(
        precheck_mod, "run_precheck",
        lambda root, base_ref, branch, timeout=600: precheck,
    )
    opener = _CrOpener(cr_results)
    monkeypatch.setattr(build_mod, "_open_story_cr", opener)
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    disp = _RecordingDispatcher(overrides)
    ledger = Ledger(tmp_path / "ledger.db")
    opts = BuildOptions(scope="epic-27", skip_preflight=True, sequential=True)
    run_build(
        opts, queue=[_story()], ledger=ledger, dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    return disp, opener, ledger


def _stage_rows(tmp_path) -> dict[str, tuple]:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    rows = conn.execute(
        "SELECT stage_name, status, failure_category FROM stages ORDER BY rowid"
    ).fetchall()
    return {name: (status, category) for name, status, category in rows}


def _events(tmp_path) -> list[str]:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    return [r[0] for r in conn.execute("SELECT message FROM events").fetchall()]


def _story_status(tmp_path) -> str:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    return conn.execute("SELECT status FROM stories").fetchone()[0]


# ---------------------------------------------------------------------------
# AC1: tests-green AND coverage >= threshold skips the coverage dispatch,
# recorded in the ledger with a skip_reason
# ---------------------------------------------------------------------------


def test_green_precheck_skips_coverage_dispatch(tmp_path, monkeypatch) -> None:
    green = PrecheckResult(tests_passed=True, coverage_pct=96.0, command="uv run pytest")
    disp, opener, _ = _run(tmp_path, monkeypatch, precheck=green)
    assert disp.calls == ["build", "review", "merge"]
    assert _stage_rows(tmp_path)["coverage"] == ("SKIPPED", "coverage-pre-check")
    assert any("skip_reason=coverage-pre-check" in m for m in _events(tmp_path))


def test_green_precheck_threads_controller_pr_to_review(tmp_path, monkeypatch) -> None:
    green = PrecheckResult(tests_passed=True, coverage_pct=96.0, command="uv run pytest")
    disp, opener, _ = _run(tmp_path, monkeypatch, precheck=green, cr_results=(300,))
    # The controller opened the CR deterministically; review runs against it.
    assert opener.calls  # the deterministic opener ran
    assert "#300" in disp.prompts["review"]
    assert _story_status(tmp_path) == "DONE"


def test_precheck_at_exact_threshold_skips(tmp_path, monkeypatch) -> None:
    exact = PrecheckResult(tests_passed=True, coverage_pct=90.0, command="uv run pytest")
    disp, _, _ = _run(tmp_path, monkeypatch, precheck=exact)
    assert "coverage" not in disp.calls


def test_green_precheck_cr_open_failure_falls_back_to_dispatch(
    tmp_path, monkeypatch
) -> None:
    # A push/host hiccup must never strand the story: the full coverage
    # dispatch runs instead, and the post-stage CR open gets a second chance.
    green = PrecheckResult(tests_passed=True, coverage_pct=96.0, command="uv run pytest")
    disp, opener, _ = _run(tmp_path, monkeypatch, precheck=green, cr_results=(None, 300))
    assert "coverage" in disp.calls
    assert _stage_rows(tmp_path)["coverage"][0] == "DONE"
    assert len(opener.calls) == 2
    assert _story_status(tmp_path) == "DONE"


# ---------------------------------------------------------------------------
# AC2: coverage below threshold or failing tests dispatches the agent as
# today, with the pre-check numbers included in its prompt
# ---------------------------------------------------------------------------


def test_below_threshold_precheck_dispatches_with_numbers(tmp_path, monkeypatch) -> None:
    low = PrecheckResult(tests_passed=True, coverage_pct=82.3, command="uv run pytest")
    disp, _, _ = _run(tmp_path, monkeypatch, precheck=low)
    assert "coverage" in disp.calls
    prompt = disp.prompts["coverage"]
    assert "82.3" in prompt
    assert "pre-check" in prompt.lower()


def test_failing_tests_precheck_dispatches_with_numbers(tmp_path, monkeypatch) -> None:
    red = PrecheckResult(tests_passed=False, coverage_pct=None, command="uv run pytest")
    disp, _, _ = _run(tmp_path, monkeypatch, precheck=red)
    assert "coverage" in disp.calls
    assert "FAILED" in disp.prompts["coverage"]


def test_inconclusive_precheck_dispatches_without_numbers(tmp_path, monkeypatch) -> None:
    # No resolvable coverage command → dispatch exactly as today (no pre-check block).
    disp, _, _ = _run(tmp_path, monkeypatch, precheck=None)
    assert "coverage" in disp.calls
    assert "pre-check" not in disp.prompts["coverage"].lower()


# ---------------------------------------------------------------------------
# AC3: the PR always exists once the coverage stage completes — opened by
# deterministic controller code, no longer by the agent
# ---------------------------------------------------------------------------


def test_agent_path_controller_opens_pr_after_coverage(tmp_path, monkeypatch) -> None:
    low = PrecheckResult(tests_passed=True, coverage_pct=50.0, command="uv run pytest")
    disp, opener, ledger = _run(tmp_path, monkeypatch, precheck=low, cr_results=(300,))
    assert len(opener.calls) == 1
    assert "#300" in disp.prompts["review"]
    conn = sqlite3.connect(tmp_path / "ledger.db")
    assert conn.execute("SELECT pr_number FROM stories").fetchone()[0] == 300


def test_agent_path_cr_open_failure_parks_needs_attention(tmp_path, monkeypatch) -> None:
    # The agent committed work but the deterministic CR open failed: park the
    # story (branch preserved, R10) rather than advance to review without a PR.
    low = PrecheckResult(tests_passed=True, coverage_pct=50.0, command="uv run pytest")
    disp, _, _ = _run(tmp_path, monkeypatch, precheck=low, cr_results=(None,))
    assert "review" not in disp.calls
    assert _story_status(tmp_path) == "NEEDS_ATTENTION"


def test_legacy_agent_reported_pr_suppresses_controller_open(tmp_path, monkeypatch) -> None:
    # A legacy agent that still reports pr_number keeps it; no double-open.
    low = PrecheckResult(tests_passed=True, coverage_pct=50.0, command="uv run pytest")
    legacy = {"coverage": {"pr_number": 555, "pr_url": "u", "coverage_pct": 95.0,
                           "tests_added": 1, "coverage_status": "PASS"}}
    disp, opener, _ = _run(tmp_path, monkeypatch, precheck=low, overrides=legacy)
    assert opener.calls == []
    assert "#555" in disp.prompts["review"]


# ---------------------------------------------------------------------------
# AC4: the gate criterion is unchanged — 90% default threshold
# ---------------------------------------------------------------------------


def test_default_threshold_is_unchanged() -> None:
    assert BuildOptions().coverage_threshold == 90


def test_passes_requires_green_tests_and_threshold() -> None:
    green = PrecheckResult(tests_passed=True, coverage_pct=95.0, command="c")
    assert passes(green, 90) is True
    assert passes(green, 96) is False
    red = PrecheckResult(tests_passed=False, coverage_pct=95.0, command="c")
    assert passes(red, 90) is False
    unmeasured = PrecheckResult(tests_passed=True, coverage_pct=None, command="c")
    assert passes(unmeasured, 90) is False


# ---------------------------------------------------------------------------
# The docs-only gate keeps precedence: no pre-check suite run for docs stories
# ---------------------------------------------------------------------------


def test_docs_only_story_never_runs_precheck(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda root, base, branch: ["README.md"]
    )

    def _boom(*args, **kwargs):
        raise AssertionError("pre-check must not run for a docs-only story")

    monkeypatch.setattr(precheck_mod, "run_precheck", _boom)
    monkeypatch.setattr(
        build_mod, "_open_docs_only_cr",
        lambda story, ledger, run_id, workdir, base_ref, close_link, cr_terms: 100,
    )
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    disp = _RecordingDispatcher()
    ledger = Ledger(tmp_path / "ledger.db")
    opts = BuildOptions(scope="epic-27", skip_preflight=True, sequential=True)
    run_build(
        opts, queue=[_story()], ledger=ledger, dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    assert _stage_rows(tmp_path)["coverage"] == ("SKIPPED", "docs-only")


# ---------------------------------------------------------------------------
# resolve_coverage_command — reuses the per-repo detection; pytest-cov only
# ---------------------------------------------------------------------------


def _pyproject(tmp_path, deps: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\ndependencies = [{deps}]\n', encoding="utf-8"
    )
    return tmp_path


def test_resolve_appends_cov_flags_for_pytest_cov_repo(tmp_path) -> None:
    _pyproject(tmp_path, '"pytest-cov>=5"')
    report = tmp_path / "cov.json"
    cmd = resolve_coverage_command(tmp_path, report)
    assert cmd is not None
    assert "pytest" in cmd
    assert "--cov=." in cmd
    assert f"--cov-report=json:{report}" in cmd


def test_resolve_without_pytest_cov_is_none(tmp_path) -> None:
    _pyproject(tmp_path, '"pytest>=8"')
    assert resolve_coverage_command(tmp_path, tmp_path / "cov.json") is None


def test_resolve_non_pytest_project_is_none(tmp_path) -> None:
    # A quality-gate script is not instrumentable for coverage measurement.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text("true\n", encoding="utf-8")
    assert resolve_coverage_command(tmp_path, tmp_path / "cov.json") is None


def test_resolve_empty_project_is_none(tmp_path) -> None:
    assert resolve_coverage_command(tmp_path, tmp_path / "cov.json") is None


# ---------------------------------------------------------------------------
# changed_files_coverage — coverage over the branch's changed files only
# ---------------------------------------------------------------------------


def _report(files: dict) -> dict:
    return {"files": {
        path: {"summary": {"num_statements": total, "covered_lines": covered}}
        for path, (total, covered) in files.items()
    }}


def test_changed_files_coverage_aggregates_changed_only() -> None:
    report = _report({"src/a.py": (10, 9), "src/b.py": (10, 1)})
    pct = changed_files_coverage(report, ["src/a.py"])
    assert pct == 90.0


def test_changed_files_coverage_ignores_unmeasured_files() -> None:
    report = _report({"src/a.py": (10, 10)})
    pct = changed_files_coverage(report, ["src/a.py", "docs/x.md", "tests/t.py"])
    assert pct == 100.0


def test_changed_files_coverage_none_when_nothing_measured() -> None:
    assert changed_files_coverage(_report({}), ["docs/x.md"]) is None
    assert changed_files_coverage({}, []) is None


# ---------------------------------------------------------------------------
# run_precheck — deterministic runner (injectable subprocess seam)
# ---------------------------------------------------------------------------


def _git_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _fake_runner(rc: int, report: dict | None):
    """A subprocess.run stand-in that writes the json report the command names."""

    def _run(cmd, **kwargs):
        if report is not None:
            path = next(
                a.split("json:", 1)[1] for a in cmd if a.startswith("--cov-report=json:")
            )
            Path(path).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    return _run


def test_run_precheck_green_suite_measures_changed_files(tmp_path, monkeypatch) -> None:
    root = _pyproject(_git_repo(tmp_path), '"pytest-cov>=5"')
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda r, base, branch: ["src/a.py"]
    )
    runner = _fake_runner(0, _report({"src/a.py": (10, 9), "src/b.py": (10, 0)}))
    result = run_precheck(root, "origin/main", "feature/x", runner=runner)
    assert result is not None
    assert result.tests_passed is True
    assert result.coverage_pct == 90.0
    assert "pytest" in result.command


def test_run_precheck_red_suite_reports_failure(tmp_path, monkeypatch) -> None:
    root = _pyproject(_git_repo(tmp_path), '"pytest-cov>=5"')
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda r, base, branch: ["src/a.py"]
    )
    runner = _fake_runner(1, None)
    result = run_precheck(root, "origin/main", "feature/x", runner=runner)
    assert result is not None
    assert result.tests_passed is False
    assert result.coverage_pct is None


def test_run_precheck_unresolvable_command_is_inconclusive(tmp_path) -> None:
    assert run_precheck(_git_repo(tmp_path), "origin/main", "feature/x") is None


def test_run_precheck_timeout_is_inconclusive(tmp_path, monkeypatch) -> None:
    root = _pyproject(_git_repo(tmp_path), '"pytest-cov>=5"')

    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert run_precheck(root, "origin/main", "feature/x", runner=_timeout) is None


def test_run_precheck_green_but_unreadable_report_is_unmeasured(
    tmp_path, monkeypatch
) -> None:
    # rc=0 with no report: never skip on absence of evidence — coverage_pct
    # stays None so passes() is False and the agent dispatches.
    root = _pyproject(_git_repo(tmp_path), '"pytest-cov>=5"')
    result = run_precheck(
        root, "origin/main", "feature/x", runner=_fake_runner(0, None)
    )
    assert result is not None
    assert result.tests_passed is True
    assert result.coverage_pct is None


def test_run_precheck_exports_recursion_sentinel(tmp_path, monkeypatch) -> None:
    root = _pyproject(_git_repo(tmp_path), '"pytest-cov>=5"')
    seen: dict = {}

    def _run(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    run_precheck(root, "origin/main", "feature/x", runner=_run)
    assert seen.get(build_mod.IN_TEST_ENV_VAR) == "1"


# ---------------------------------------------------------------------------
# Prompt contract: the coverage agent no longer pushes or opens the PR
# ---------------------------------------------------------------------------


def test_coverage_prompt_drops_pr_creation_instructions() -> None:
    prompt = build_mod.render_coverage_prompt(_story(), BuildOptions())
    assert "Push, open the" not in prompt
    assert "the controller pushes and opens the PR" in prompt


def test_coverage_prompt_gitlab_names_the_mr() -> None:
    from sdlc.issue_host import GITLAB_CR_TERMS

    prompt = build_mod.render_coverage_prompt(
        _story(), BuildOptions(), cr_terms=GITLAB_CR_TERMS
    )
    assert "the controller pushes and opens the MR" in prompt


def test_coverage_prompt_embeds_precheck_numbers() -> None:
    low = PrecheckResult(tests_passed=True, coverage_pct=82.3, command="uv run pytest --cov")
    prompt = build_mod.render_coverage_prompt(_story(), BuildOptions(), precheck=low)
    assert "82.3" in prompt
    assert "uv run pytest --cov" in prompt
    assert "90%" in prompt


def test_build_prompt_hands_off_pr_to_controller() -> None:
    prompt = build_mod.render_build_prompt(_story(), BuildOptions())
    assert "the controller pushes and opens the PR" in prompt


# ---------------------------------------------------------------------------
# Schema: pr_number/pr_url are no longer required (controller owns the PR)
# ---------------------------------------------------------------------------


def test_coverage_schema_accepts_response_without_pr_fields() -> None:
    from sdlc.contracts import validate_response

    data = {"coverage_pct": 95.0, "tests_added": 2, "coverage_status": "PASS"}
    assert validate_response("coverage", data) == data
