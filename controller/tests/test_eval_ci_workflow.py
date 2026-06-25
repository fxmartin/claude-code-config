# ABOUTME: Tests for the CI eval workflow (Story 18.1-003) — asserts the job is
# ABOUTME: path-filtered to agent-affecting changes, quota-bounded, and baseline-gated.

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "eval-ci.yml"


def _load_workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_workflow_exists() -> None:
    assert _WORKFLOW.is_file(), f"missing CI eval workflow: {_WORKFLOW}"


def test_workflow_is_valid_yaml_with_a_job() -> None:
    wf = _load_workflow()
    assert wf.get("jobs"), "workflow must define at least one job"


def test_workflow_runs_only_on_pull_requests_with_a_path_filter() -> None:
    # PyYAML parses the bare `on:` key as the boolean True; tolerate either form.
    wf = _load_workflow()
    triggers = wf.get("on", wf.get(True))
    assert isinstance(triggers, dict), "workflow must use the mapping form of `on:`"
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "eval must trigger on pull_request"
    paths = pull_request.get("paths")
    assert paths, "pull_request must be path-filtered so unrelated PRs skip the eval"


def test_path_filter_covers_prompts_skills_and_schemas() -> None:
    wf = _load_workflow()
    triggers = wf.get("on", wf.get(True))
    paths = " ".join(triggers["pull_request"]["paths"])
    # Agent prompts (build.py renders the build-agent prompt), skills, and the
    # response schemas are the three change classes the story names.
    assert "build.py" in paths, "path filter must cover the build-agent prompt"
    assert "skills/" in paths, "path filter must cover skills"
    assert "schemas/" in paths, "path filter must cover agent response schemas"
    assert "eval/" in paths, "path filter must cover the eval config/tickets"


def test_workflow_runs_the_bounded_ci_eval() -> None:
    text = _workflow_text()
    assert "ci-config.yaml" in text, "CI must run the bounded ci-config, not the full eval"
    assert "sdlc eval" in text, "CI must invoke the eval harness"


def test_workflow_checks_against_the_baseline() -> None:
    text = _workflow_text()
    assert "eval-baseline" in text, "CI must check the scoreboard against the baseline"


def test_workflow_warn_or_fail_is_configurable() -> None:
    # The story requires warn-vs-fail to be configurable; we drive it from a repo
    # variable so flipping advisory -> blocking needs no code change.
    text = _workflow_text()
    assert "EVAL_CI_WARN_ONLY" in text, "warn/fail must be configurable via a variable"
    assert "--warn-only" in text, "advisory mode must be wired to eval-baseline --warn-only"


def test_workflow_is_quota_gated_on_a_secret() -> None:
    # The eval spends real quota on Max; with no credentials (forks, no secret) the
    # job must skip cleanly rather than fail.
    text = _workflow_text()
    assert "ANTHROPIC_API_KEY" in text, "eval must be gated on the model credential"


def test_workflow_has_a_timeout() -> None:
    wf = _load_workflow()
    jobs = wf["jobs"]
    assert any(
        "timeout-minutes" in job for job in jobs.values()
    ), "the eval job must set timeout-minutes as a quota/runtime backstop"
