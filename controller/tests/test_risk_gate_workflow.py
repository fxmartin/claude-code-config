# ABOUTME: Tests that the risk-gate CI workflow evaluates the detector and its
# ABOUTME: policy from the trusted base-ref checkout, not the PR's own (issue #640).

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "risk-gate.yml"


def _load_workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    workflow = _load_workflow()
    return workflow["jobs"]["risk-gate"]["steps"]


def _step_named(name: str) -> dict:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in {_WORKFLOW}")


def test_workflow_exists() -> None:
    assert _WORKFLOW.is_file(), f"missing risk-gate workflow: {_WORKFLOW}"


def test_trusted_base_ref_checkout_is_pinned_to_the_pr_base_sha() -> None:
    # Regression for issue #640: the trusted checkout must never resolve to
    # the PR head, or the "trusted" copy is just the attacker's copy again.
    step = _step_named("Checkout trusted base ref")
    assert step["uses"].startswith("actions/checkout@")
    with_args = step["with"]
    assert with_args["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert with_args["path"] == "base-ref"


def test_trusted_checkout_precedes_the_detect_step() -> None:
    names = [step.get("name") for step in _steps()]
    assert names.index("Checkout trusted base ref") < names.index(
        "Detect high-risk changed files"
    )


def test_detect_step_runs_the_detector_from_the_trusted_checkout() -> None:
    # Regression for issue #640: a PR that edits the detector script or its
    # policy in its own checkout must not be able to influence which code
    # or patterns run against it. Only the (inert) changed-file names may
    # come from the PR checkout.
    run = _step_named("Detect high-risk changed files")["run"]
    assert "base-ref/scripts/risk-gate-detect.sh base-ref" in run


def test_detect_step_never_invokes_the_untrusted_pr_checkout_script() -> None:
    run = _step_named("Detect high-risk changed files")["run"]
    assert "bash scripts/risk-gate-detect.sh" not in run


def test_workflow_config_file_and_detector_script_are_self_protected() -> None:
    # Belt-and-suspenders alongside the trusted checkout: the policy and the
    # detector must be high-risk paths in their own right.
    config = yaml.safe_load(
        (
            _REPO_ROOT
            / "controller"
            / "src"
            / "sdlc"
            / "config"
            / "high-risk-patterns.yaml"
        ).read_text(encoding="utf-8")
    )
    patterns = config["high_risk_patterns"]
    assert "controller/src/sdlc/config/high-risk-patterns.yaml" in patterns
    assert "scripts/risk-gate-detect.sh" in patterns
