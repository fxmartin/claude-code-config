# ABOUTME: Tests for the GitLab adoption preflight — glab auth, project, CI, gate template.
# ABOUTME: Story 23.6-002. Each missing prerequisite must surface as its own FAIL finding.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.gitlab_preflight import (
    CHECK,
    GATE_TEMPLATE,
    ProjectInfo,
    check_ci,
    check_gate_template,
    check_glab_auth,
    check_glab_installed,
    check_project,
    project_info_from_json,
    run_gitlab_preflight,
)

runner = CliRunner()


# --- project_info_from_json -------------------------------------------------


def test_project_info_from_json_reads_builds_access_level() -> None:
    info = project_info_from_json(
        {
            "path_with_namespace": "acme/widgets",
            "default_branch": "main",
            "builds_access_level": "enabled",
        }
    )
    assert info == ProjectInfo(path="acme/widgets", default_branch="main", ci_enabled=True)


def test_project_info_from_json_disabled_builds_access_level() -> None:
    info = project_info_from_json(
        {
            "path_with_namespace": "acme/widgets",
            "default_branch": "main",
            "builds_access_level": "disabled",
        }
    )
    assert info.ci_enabled is False


def test_project_info_from_json_falls_back_to_jobs_enabled() -> None:
    info = project_info_from_json(
        {"path_with_namespace": "acme/widgets", "default_branch": "main", "jobs_enabled": False}
    )
    assert info.ci_enabled is False


def test_project_info_from_json_defaults_ci_enabled_when_field_absent() -> None:
    # GitLab always emits a CI field, but a missing field must not produce a
    # false "disabled" FAIL — default to enabled.
    info = project_info_from_json({"path_with_namespace": "acme/widgets", "default_branch": "main"})
    assert info.ci_enabled is True


# --- check_glab_installed ----------------------------------------------------


def test_check_glab_installed_clean_when_present() -> None:
    assert check_glab_installed(True).status == "CLEAN"


def test_check_glab_installed_fails_when_absent() -> None:
    finding = check_glab_installed(False)
    assert finding.status == "FAIL"
    assert finding.remedy  # actionable install hint


# --- check_glab_auth ---------------------------------------------------------


def test_check_glab_auth_clean_with_login() -> None:
    finding = check_glab_auth("fx", None)
    assert finding.status == "CLEAN"
    assert "fx" in finding.detail


def test_check_glab_auth_fails_with_error() -> None:
    finding = check_glab_auth(None, "not authenticated to gitlab")
    assert finding.status == "FAIL"
    assert "glab auth login" in finding.remedy


# --- check_project -----------------------------------------------------------


def test_check_project_clean() -> None:
    finding = check_project(ProjectInfo("acme/widgets", "main", True), None)
    assert finding.status == "CLEAN"
    assert "acme/widgets" in finding.detail
    assert "main" in finding.detail


def test_check_project_fails_when_unresolved() -> None:
    finding = check_project(None, "project not found")
    assert finding.status == "FAIL"


def test_check_project_fails_without_default_branch() -> None:
    finding = check_project(ProjectInfo("acme/widgets", None, True), None)
    assert finding.status == "FAIL"
    assert "default branch" in finding.detail.lower()


# --- check_ci ----------------------------------------------------------------


def test_check_ci_clean_when_enabled() -> None:
    assert check_ci(ProjectInfo("acme/widgets", "main", True), None).status == "CLEAN"


def test_check_ci_fails_when_disabled() -> None:
    finding = check_ci(ProjectInfo("acme/widgets", "main", False), None)
    assert finding.status == "FAIL"
    assert "CI" in finding.detail


def test_check_ci_fails_when_project_unresolved() -> None:
    assert check_ci(None, "could not resolve project").status == "FAIL"


# --- check_gate_template -----------------------------------------------------


def test_check_gate_template_clean_when_present(tmp_path: Path) -> None:
    (tmp_path / GATE_TEMPLATE).write_text("stages: [test]\n", encoding="utf-8")
    assert check_gate_template(tmp_path).status == "CLEAN"


def test_check_gate_template_fails_when_missing(tmp_path: Path) -> None:
    finding = check_gate_template(tmp_path)
    assert finding.status == "FAIL"
    assert GATE_TEMPLATE in finding.detail


# --- run_gitlab_preflight (orchestration) -----------------------------------


def _healthy_target(tmp_path: Path) -> Path:
    (tmp_path / GATE_TEMPLATE).write_text("stages: [test]\n", encoding="utf-8")
    return tmp_path


def test_run_gitlab_preflight_all_clean(tmp_path: Path) -> None:
    report = run_gitlab_preflight(
        repo_root=_healthy_target(tmp_path),
        which=lambda _: "/usr/bin/glab",
        glab_auth=lambda: "fx",
        project_probe=lambda: ProjectInfo("acme/widgets", "main", True),
    )
    assert report.status == "CLEAN"
    assert all(f.check == CHECK for f in report.findings)
    # Every prerequisite dimension is reported.
    names = {f.name for f in report.findings}
    assert len(names) == 5


def test_run_gitlab_preflight_glab_missing_fails_live_checks(tmp_path: Path) -> None:
    report = run_gitlab_preflight(
        repo_root=_healthy_target(tmp_path),
        which=lambda _: None,
        glab_auth=lambda: "fx",  # must not be consulted when glab is absent
        project_probe=lambda: ProjectInfo("acme/widgets", "main", True),
    )
    assert report.status == "FAIL"
    statuses = {f.name: f.status for f in report.findings}
    # install, auth, project, ci all FAIL; template still CLEAN (filesystem).
    fails = [name for name, status in statuses.items() if status == "FAIL"]
    assert len(fails) == 4


def test_run_gitlab_preflight_auth_failure_skips_project(tmp_path: Path) -> None:
    from sdlc.host_auth import HostAuthError

    def _raise() -> str:
        raise HostAuthError("not authenticated to gitlab; run `glab auth login`")

    probe_calls = {"n": 0}

    def _probe() -> ProjectInfo:
        probe_calls["n"] += 1
        return ProjectInfo("acme/widgets", "main", True)

    report = run_gitlab_preflight(
        repo_root=_healthy_target(tmp_path),
        which=lambda _: "/usr/bin/glab",
        glab_auth=_raise,
        project_probe=_probe,
    )
    assert report.status == "FAIL"
    # The project probe is never run once auth fails — no point hitting the API.
    assert probe_calls["n"] == 0


def test_run_gitlab_preflight_ci_disabled_fails(tmp_path: Path) -> None:
    report = run_gitlab_preflight(
        repo_root=_healthy_target(tmp_path),
        which=lambda _: "/usr/bin/glab",
        glab_auth=lambda: "fx",
        project_probe=lambda: ProjectInfo("acme/widgets", "main", False),
    )
    ci = next(f for f in report.findings if "CI" in f.name)
    assert ci.status == "FAIL"


def test_run_gitlab_preflight_missing_template_fails(tmp_path: Path) -> None:
    report = run_gitlab_preflight(
        repo_root=tmp_path,  # no .gitlab-ci.yml
        which=lambda _: "/usr/bin/glab",
        glab_auth=lambda: "fx",
        project_probe=lambda: ProjectInfo("acme/widgets", "main", True),
    )
    template = next(f for f in report.findings if GATE_TEMPLATE in f.detail)
    assert template.status == "FAIL"


def test_run_gitlab_preflight_project_probe_error_fails(tmp_path: Path) -> None:
    from sdlc.issue_host import IssueHostError

    def _raise() -> ProjectInfo:
        raise IssueHostError("glab api projects/:id failed: 404 Not Found")

    report = run_gitlab_preflight(
        repo_root=_healthy_target(tmp_path),
        which=lambda _: "/usr/bin/glab",
        glab_auth=lambda: "fx",
        project_probe=_raise,
    )
    project = next(f for f in report.findings if f.name.lower().startswith("gitlab project"))
    assert project.status == "FAIL"


# --- CLI wiring --------------------------------------------------------------


def test_cli_doctor_gitlab_includes_preflight_findings(tmp_path: Path) -> None:
    # A target without the gate template guarantees at least one gitlab FAIL,
    # independent of whether glab is installed in the test environment.
    result = runner.invoke(
        app,
        ["doctor", "--gitlab", "--target", str(tmp_path), "--json", "--claude-dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert any(f["check"] == CHECK for f in payload["findings"])


def test_cli_doctor_gitlab_exit_code_flag_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["doctor", "--gitlab", "--target", str(tmp_path), "--exit-code", "--claude-dir", str(tmp_path)],
    )
    # Missing gate template → FAIL → exit 2 under --exit-code.
    assert result.exit_code == 2
