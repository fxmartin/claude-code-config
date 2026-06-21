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


def test_high_risk_story_escalates_review_to_opus(tmp_path, monkeypatch) -> None:
    # The file-based risk signal is consulted only for review (its diff is stable
    # at decision time); build escalates on points alone, so a small high-risk
    # story keeps build on Sonnet but escalates review to Opus.
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: True)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["review"] == OPUS
    assert disp.models["build"] == SONNET  # build escalates on points, not live risk


def test_build_model_is_deterministic_regardless_of_branch_risk(
    tmp_path, monkeypatch
) -> None:
    """Resume safety: build's model never depends on the live-git risk signal.

    _story_high_risk reads live git state (the branch exists on resume but not on
    a fresh build's build stage). Routing build off it would flip Sonnet→Opus
    across a resume. Build must ignore it, so even a True risk verdict leaves a
    small story's build on Sonnet.
    """
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: True)
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    disp = _run(opts, _story(points=1), tmp_path)
    assert disp.models["build"] == SONNET


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


def test_parse_rejects_overrides_for_unrouted_stages() -> None:
    """discovery / adversarial are dispatched outside this pipeline — overriding
    their model here would be a silent no-op, so the parse rejects it."""
    import pytest

    for stage in ("discovery", "adversarial"):
        with pytest.raises(ValueError, match="unknown stage"):
            parse_build_args([f"--model-{stage}=opus"])


def test_parse_accepts_recovery_stage_overrides() -> None:
    opts = parse_build_args(["--model-bugfix=opus", "--model-reask=haiku"])
    assert opts.model_overrides == {"bugfix": "opus", "reask": "haiku"}


def test_parse_per_stage_override_without_value_raises() -> None:
    """`--model-build` with no `=value` is a typo, not a silent no-op — the parse
    fails eagerly before the stage is ever validated."""
    import pytest

    with pytest.raises(ValueError, match="needs a value"):
        parse_build_args(["--model-build"])


# ---------------------------------------------------------------------------
# Recovery stages (bugfix / reask) receive routed models
# ---------------------------------------------------------------------------


class _BugfixRecordingDispatcher:
    """Fails build once, then records the model of every dispatched agent.

    Records each call as (agent_type, model) so the bugfix agent's routed model
    can be asserted distinctly from the build agent's.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._build_seen = 0

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append((agent_type, kwargs.get("model")))
        if agent_type == "build":
            self._build_seen += 1
            if self._build_seen == 1:
                return AgentResult(
                    agent_type="build",
                    data={"branch_name": "feature/x", "build_status": "FAILED",
                          "error_summary": "boom"},
                    raw="",
                )
        if agent_type == "bugfix":
            return AgentResult(
                agent_type="bugfix",
                data={"failure_category": "TEST_BUG", "fix_status": "FIXED",
                      "tests_passing": True, "bugs_fixed": 1, "tests_fixed": 0},
                raw="",
            )
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")


def test_bugfix_stage_receives_routed_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    disp = _BugfixRecordingDispatcher()
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced",
    )
    run_build(
        opts, queue=[_story(points=1)], ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp, preflight=lambda: True,
    )
    bugfix_models = [m for (a, m) in disp.calls if a == "bugfix"]
    assert bugfix_models, "bugfix agent was never dispatched"
    assert bugfix_models[0] == SONNET  # balanced 'bugfix' tier, not the CLI default


def test_bugfix_override_wins_over_map(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    disp = _BugfixRecordingDispatcher()
    opts = BuildOptions(
        scope="epic-14", skip_preflight=True, sequential=True,
        model_profile="balanced", model_overrides={"bugfix": "haiku"},
    )
    run_build(
        opts, queue=[_story(points=1)], ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp, preflight=lambda: True,
    )
    bugfix_models = [m for (a, m) in disp.calls if a == "bugfix"]
    assert bugfix_models[0] == HAIKU


# ---------------------------------------------------------------------------
# _story_high_risk — best-effort risk signal (Story 14.2-001)
# ---------------------------------------------------------------------------


def _completed(stdout: str):
    """A minimal stand-in for subprocess.run's CompletedProcess (only .stdout)."""
    import types

    return types.SimpleNamespace(stdout=stdout)


def test_story_high_risk_off_short_circuits(monkeypatch) -> None:
    """Routing off never shells out — the probe returns False before touching git."""

    def _must_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when routing is off")

    monkeypatch.setattr(build_mod.subprocess, "run", _must_not_run)
    for profile in ("", "off", "none", "  OFF  "):
        opts = BuildOptions(model_profile=profile)
        assert build_mod._story_high_risk(_story(), opts) is False


def test_story_high_risk_true_when_changed_file_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        build_mod.subprocess, "run",
        lambda *a, **k: _completed("darwin/configuration.nix\n"),
    )
    monkeypatch.setattr(
        "sdlc.risk_gate.match_high_risk",
        lambda files, **k: {"darwin/configuration.nix": "darwin/**"},
    )
    opts = BuildOptions(model_profile="balanced")
    assert build_mod._story_high_risk(_story(), opts) is True


def test_story_high_risk_false_when_no_changed_files(monkeypatch) -> None:
    """An empty diff (whitespace-only stdout) never consults the risk patterns."""

    def _must_not_match(*args, **kwargs):
        raise AssertionError("match_high_risk must not run with no changed files")

    monkeypatch.setattr(build_mod.subprocess, "run", lambda *a, **k: _completed("\n  \n"))
    monkeypatch.setattr("sdlc.risk_gate.match_high_risk", _must_not_match)
    opts = BuildOptions(model_profile="balanced")
    assert build_mod._story_high_risk(_story(), opts) is False


def test_story_high_risk_false_when_no_pattern_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        build_mod.subprocess, "run", lambda *a, **k: _completed("README.md\n")
    )
    monkeypatch.setattr("sdlc.risk_gate.match_high_risk", lambda files, **k: {})
    opts = BuildOptions(model_profile="balanced")
    assert build_mod._story_high_risk(_story(), opts) is False


def test_story_high_risk_degrades_to_false_on_error(monkeypatch) -> None:
    """Any git/import failure degrades to False so routing never fails a build."""

    def _raise(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(build_mod.subprocess, "run", _raise)
    opts = BuildOptions(model_profile="balanced")
    assert build_mod._story_high_risk(_story(), opts) is False
