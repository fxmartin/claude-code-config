# ABOUTME: Tests the build state machine's per-task model routing wiring (Story 14.2-001).
# ABOUTME: Asserts which --model each pipeline stage is dispatched with under each profile.

from __future__ import annotations

import sdlc.build as build_mod
from sdlc.build import (
    BuildOptions,
    Ledger,
    _select_stage_model,
    parse_build_args,
    run_build,
)
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.model_routing import HAIKU, OPUS, SONNET

_PAYLOADS = {
    "build": {"branch_name": "feature/x", "build_status": "SUCCESS", "commit_sha": "a"},
    "coverage": {
        "pr_number": 100, "pr_url": "u", "coverage_pct": 95.0, "tests_added": 1,
        "coverage_status": "PASS", "security_status": "PASS",
    },
    "review": {"pr_number": 100, "approval_status": "APPROVED", "change_count": 0,
               "final_status": "APPROVED"},
    "merge": {"pr_number": 100, "merge_status": "MERGED", "merge_sha": "b",
              "merged_at": "2026-06-21T00:00:00Z"},
}


class _ModelRecordingDispatcher:
    """Records (stage → model) for each dispatch and returns a canned success."""

    def __init__(self) -> None:
        self.models: dict[str, str | None] = {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.models[agent_type] = kwargs.get("model")
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")


def _story(points: int = 1) -> Story:
    return Story(
        id="14.2-001", title="t", epic_id="epic-14", epic_name="e",
        epic_file="f.md", priority="Should", points=points, agent_type="python",
    )


def _run(opts: BuildOptions, story: Story, tmp_path) -> _ModelRecordingDispatcher:
    disp = _ModelRecordingDispatcher()
    run_build(
        opts,
        queue=[story],
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    return disp


# ---------------------------------------------------------------------------
# Routing off (default) = unchanged behaviour: no --model on any stage
# ---------------------------------------------------------------------------


def test_routing_off_by_default_dispatches_no_model(tmp_path) -> None:
    opts = BuildOptions(scope="epic-14", skip_preflight=True, sequential=True)
    disp = _run(opts, _story(), tmp_path)
    assert disp.models == {"build": None, "coverage": None, "review": None, "merge": None}


# ---------------------------------------------------------------------------
# Balanced profile: the per-stage default map for a small, low-risk story
# ---------------------------------------------------------------------------


def test_balanced_profile_routes_each_stage(tmp_path, monkeypatch) -> None:
    # A small, low-risk story stays on the cheap tiers.
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["build"] == SONNET
    assert disp.models["coverage"] == SONNET
    assert disp.models["review"] == SONNET
    assert disp.models["merge"] == HAIKU


def test_large_story_escalates_build_and_review_to_opus(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    disp = _run(opts, _story(points=13), tmp_path)
    assert disp.models["build"] == OPUS
    assert disp.models["review"] == OPUS
    # Non-escalatable stages keep their cheap tier even for a large story.
    assert disp.models["coverage"] == SONNET
    assert disp.models["merge"] == HAIKU


def test_high_risk_story_escalates_build_and_review_to_opus(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: True)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["build"] == OPUS
    assert disp.models["review"] == OPUS


# ---------------------------------------------------------------------------
# Override precedence: an explicit --model-<stage> wins over the map
# ---------------------------------------------------------------------------


def test_explicit_per_stage_override_wins_over_map(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced", model_overrides={"merge": "opus"},
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["merge"] == OPUS  # override beats the HAIKU default
    assert disp.models["build"] == SONNET  # other stages still from the map


def test_override_works_even_with_routing_off(tmp_path, monkeypatch) -> None:
    """An explicit per-stage --model is honoured even when no profile is set."""
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_overrides={"build": "haiku"},
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["build"] == HAIKU
    assert disp.models["coverage"] is None  # unset stage stays CLI-default


# ---------------------------------------------------------------------------
# _select_stage_model precedence (unit-level)
# ---------------------------------------------------------------------------


def test_select_stage_model_off_returns_none() -> None:
    opts = BuildOptions()
    assert _select_stage_model("build", _story(), opts) is None


def test_select_stage_model_override_beats_profile() -> None:
    opts = BuildOptions(model_profile="balanced", model_overrides={"build": "opus"})
    assert _select_stage_model("build", _story(points=1), opts) == OPUS


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_model_routing_profile() -> None:
    opts = parse_build_args(["epic-14", "--model-routing=balanced"])
    assert opts.model_profile == "balanced"


def test_parse_unknown_profile_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown model-routing profile"):
        parse_build_args(["--model-routing=turbo"])


def test_parse_per_stage_override() -> None:
    opts = parse_build_args(["epic-14", "--model-build=opus", "--model-merge=haiku"])
    assert opts.model_overrides == {"build": "opus", "merge": "haiku"}


def test_parse_unknown_stage_override_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown stage"):
        parse_build_args(["--model-bogus=opus"])


def test_parse_routing_off_is_default() -> None:
    opts = parse_build_args(["epic-14"])
    assert opts.model_profile == ""
    assert opts.model_overrides == {}
