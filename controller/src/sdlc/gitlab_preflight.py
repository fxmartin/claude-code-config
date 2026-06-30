# ABOUTME: GitLab adoption preflight — `sdlc doctor --gitlab` verifies a target repo is build-ready.
# ABOUTME: Story 23.6-002. Checks glab auth, the project + default branch, CI enabled, and the gate template.

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sdlc.doctor import DoctorReport, Finding
from sdlc.host_auth import HostAuthError, resolve_local_login
from sdlc.issue_host import GitLabAdapter, IssueHostError

__all__ = [
    "CHECK",
    "GATE_TEMPLATE",
    "ProjectInfo",
    "project_info_from_json",
    "check_glab_installed",
    "check_glab_auth",
    "check_project",
    "check_ci",
    "check_gate_template",
    "run_gitlab_preflight",
]

# Finding category id for every preflight result, so a consumer can filter the
# GitLab-target findings out of a combined `doctor` report.
CHECK = "gitlab"

# The installable quality-gate file the adopter copies from `templates/gitlab-ci.yml`
# (Story 23.3-001). Its presence at the target repo root is the last preflight gate.
GATE_TEMPLATE = ".gitlab-ci.yml"

# `glab api` substitutes `:id` with the URL-encoded path of the project the
# current git remote points at, so this one call resolves the adopter's project
# without them having to name it.
_PROJECT_API_PATH = "projects/:id"
_GLAB_TIMEOUT = 30.0


@dataclass(frozen=True)
class ProjectInfo:
    """The slice of a GitLab project the preflight needs.

    ``path`` is ``path_with_namespace`` (e.g. ``acme/widgets``); ``default_branch``
    is the project's default branch (``None`` when the project has no commits yet);
    ``ci_enabled`` is whether CI/CD is on so a pipeline can gate the MR.
    """

    path: str | None
    default_branch: str | None
    ci_enabled: bool


def project_info_from_json(data: dict) -> ProjectInfo:
    """Build :class:`ProjectInfo` from a GitLab project API object.

    CI status reads ``builds_access_level`` (``enabled``/``private``/``disabled``)
    first, falling back to the legacy boolean ``jobs_enabled``. When neither field
    is present the project is treated as CI-enabled — a missing field must not
    masquerade as "CI disabled" and block adoption falsely.
    """
    builds = data.get("builds_access_level")
    if isinstance(builds, str):
        ci_enabled = builds.lower() != "disabled"
    elif "jobs_enabled" in data:
        ci_enabled = bool(data.get("jobs_enabled"))
    else:
        ci_enabled = True
    return ProjectInfo(
        path=data.get("path_with_namespace"),
        default_branch=data.get("default_branch"),
        ci_enabled=ci_enabled,
    )


# --- individual checks (pure; resolved inputs in, Finding out) ---------------


def check_glab_installed(present: bool) -> Finding:
    """The GitLab CLI must be on PATH for every other live check to run."""
    if present:
        return Finding(CHECK, "glab installed", "CLEAN", "glab is on PATH")
    return Finding(
        CHECK,
        "glab installed",
        "FAIL",
        "glab not found on PATH",
        "install the GitLab CLI — https://gitlab.com/gitlab-org/cli (`brew install glab`)",
    )


def check_glab_auth(login: str | None, error: str | None) -> Finding:
    """The CLI must be authenticated so the build loop can act as the developer."""
    if login:
        return Finding(CHECK, "glab authenticated", "CLEAN", f"authenticated as {login}")
    return Finding(
        CHECK,
        "glab authenticated",
        "FAIL",
        error or "glab is not authenticated",
        "run `glab auth login`",
    )


def check_project(info: ProjectInfo | None, error: str | None) -> Finding:
    """The target project (and its default branch) must resolve from the remote."""
    if info is None:
        return Finding(
            CHECK,
            "GitLab project",
            "FAIL",
            error or "project could not be resolved",
            "check the git remote points at the GitLab project, then `glab auth login`",
        )
    if not info.default_branch:
        return Finding(
            CHECK,
            "GitLab project",
            "FAIL",
            f"project {info.path} resolved but its default branch is unknown",
            "push an initial commit so the project has a default branch",
        )
    return Finding(
        CHECK,
        "GitLab project",
        "CLEAN",
        f"project {info.path} (default branch: {info.default_branch})",
    )


def check_ci(info: ProjectInfo | None, error: str | None) -> Finding:
    """CI/CD must be enabled so the gate pipeline can run and block a red MR."""
    if info is None:
        return Finding(
            CHECK,
            "GitLab CI enabled",
            "FAIL",
            error or "CI status could not be determined",
            "resolve project access first (see the GitLab project check)",
        )
    if not info.ci_enabled:
        return Finding(
            CHECK,
            "GitLab CI enabled",
            "FAIL",
            f"CI/CD is disabled for project {info.path}",
            "enable CI/CD in the project settings (Settings → General → Visibility → CI/CD)",
        )
    return Finding(CHECK, "GitLab CI enabled", "CLEAN", "CI/CD is enabled")


def check_gate_template(repo_root: Path) -> Finding:
    """The installable `.gitlab-ci.yml` gate template must be present at the repo root."""
    if (repo_root / GATE_TEMPLATE).is_file():
        return Finding(CHECK, "Gate template", "CLEAN", f"{GATE_TEMPLATE} present")
    return Finding(
        CHECK,
        "Gate template",
        "FAIL",
        f"{GATE_TEMPLATE} missing from {repo_root}",
        f"cp templates/gitlab-ci.yml {repo_root}/{GATE_TEMPLATE} (Story 23.3-001)",
    )


# --- default live seams (production; injected/stubbed in tests) --------------


def _default_glab_auth() -> str:
    """Resolve the developer's `glab` login, raising :class:`HostAuthError` on failure."""
    return resolve_local_login(GitLabAdapter())


def _default_project_probe() -> ProjectInfo:
    """Fetch the current repo's GitLab project via `glab api projects/:id`.

    Raises :class:`IssueHostError` when the CLI is absent, the call fails (no
    project, not authorised), or the response is not JSON — the orchestrator maps
    that to a FAIL finding.
    """
    try:
        proc = subprocess.run(
            ["glab", "api", _PROJECT_API_PATH],
            capture_output=True,
            text=True,
            timeout=_GLAB_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise IssueHostError("glab not found on PATH — install the GitLab CLI") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise IssueHostError(f"glab api {_PROJECT_API_PATH} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise IssueHostError(f"glab api {_PROJECT_API_PATH} failed: {detail}")
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise IssueHostError(f"could not parse glab project JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IssueHostError("glab project response was not a JSON object")
    return project_info_from_json(data)


# --- orchestration -----------------------------------------------------------


def run_gitlab_preflight(
    *,
    repo_root: Path,
    which: Callable[[str], str | None] = shutil.which,
    glab_auth: Callable[[], str] | None = None,
    project_probe: Callable[[], ProjectInfo] | None = None,
) -> DoctorReport:
    """Run every GitLab-target adoption check and aggregate them into a report.

    Read-only: it inspects the target repo, the `glab` CLI auth, and the project's
    GitLab settings, reporting a remedy for each gap. All live seams (`which`, the
    auth resolver, the project probe) are injectable so the checks are testable
    without a live `glab` (Story 23.6-002 — preflight detects each missing
    prerequisite). The live checks short-circuit: a missing CLI skips auth/project,
    and a failed auth skips the project API call (no point hitting it unauthorised).
    """
    glab_auth = glab_auth or _default_glab_auth
    project_probe = project_probe or _default_project_probe

    present = which("glab") is not None

    login: str | None = None
    login_error: str | None = None
    info: ProjectInfo | None = None
    info_error: str | None = None

    if not present:
        login_error = "glab is not installed"
        info_error = "glab is not installed"
    else:
        try:
            login = glab_auth()
        except HostAuthError as exc:
            login_error = str(exc)
        if login:
            try:
                info = project_probe()
            except IssueHostError as exc:
                info_error = str(exc)
        else:
            info_error = "skipped — glab is not authenticated"

    findings = [
        check_glab_installed(present),
        check_glab_auth(login, login_error),
        check_project(info, info_error),
        check_ci(info, info_error),
        check_gate_template(repo_root),
    ]
    return DoctorReport(findings=findings)
