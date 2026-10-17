# ABOUTME: Tests the per-stage effort ceiling on retry escalation (Story 34.5-003).
# ABOUTME: Covers default ceilings, held-back event, YAML override + rejections, banner, parity.

from __future__ import annotations

import sqlite3

import pytest

import sdlc.build as build_mod
from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.model_routing import (
    BALANCED,
    BALANCED_EFFORT_CEILING,
    QUALITY_FIRST,
    config_from_snapshot,
    escalate_effort,
    escalate_model,
    load_routing_config,
    routing_banner,
    routing_snapshot,
    select_effort,
)
from sdlc.status import format_routing

_OPUS = "claude-opus-5-5"


def _cfg(yaml_body: str = ""):
    text = "model_routing:\n" + yaml_body if yaml_body else None
    cfg = load_routing_config("balanced", override_text=text)
    assert cfg is not None
    return cfg


# --- defaults ---------------------------------------------------------------


def test_default_ceilings_match_spec() -> None:
    assert BALANCED_EFFORT_CEILING == {
        "review": "xhigh", "bugfix": "xhigh", "build": "high",
        "discovery": "medium", "docs": "medium", "coverage": "medium", "merge": "medium",
    }
    assert BALANCED.effort_ceilings == BALANCED_EFFORT_CEILING


def test_every_default_ceiling_is_at_or_above_its_base() -> None:
    from sdlc.model_routing import EFFORT_LEVELS

    for stage, base in BALANCED.stage_efforts.items():
        assert EFFORT_LEVELS.index(BALANCED_EFFORT_CEILING[stage]) >= EFFORT_LEVELS.index(base)


def test_other_profiles_carry_no_ceilings() -> None:
    assert QUALITY_FIRST.effort_ceilings == {}


def test_escalate_effort_respects_ceiling() -> None:
    assert escalate_effort("high", 3, ceiling="xhigh") == "xhigh"
    assert escalate_effort("high", 1, ceiling="xhigh") == "xhigh"
    assert escalate_effort("high", 3) == "max"  # no ceiling: unchanged


@pytest.mark.parametrize(
    ("stage", "ceiling"),
    [("review", "xhigh"), ("bugfix", "xhigh"), ("build", "high"), ("coverage", "medium"),
     ("merge", "medium"), ("discovery", "medium"), ("docs", "medium")],
)
def test_climb_stops_at_default_ceiling(stage: str, ceiling: str) -> None:
    choice = select_effort(stage, BALANCED, _OPUS, escalation_steps=6)
    assert choice.level == ceiling


def test_below_ceiling_climbs_one_per_step() -> None:
    assert select_effort("bugfix", BALANCED, _OPUS, escalation_steps=0).level == "high"
    assert select_effort("bugfix", BALANCED, _OPUS, escalation_steps=1).level == "xhigh"
    assert select_effort("bugfix", BALANCED, _OPUS, escalation_steps=2).level == "xhigh"


def test_first_attempt_is_never_held() -> None:
    choice = select_effort("review", BALANCED, _OPUS)
    assert choice.level == "xhigh" and choice.held == ""


# --- held-back event text ---------------------------------------------------


def test_held_text_names_ceiling_stage_would_be_and_default_source() -> None:
    choice = select_effort("review", BALANCED, _OPUS, escalation_steps=1)
    assert choice.held == (
        "effort held at xhigh for review (would have been max; ceiling from default)"
    )


def test_held_text_names_yaml_source_for_an_overridden_ceiling() -> None:
    cfg = _cfg("  effort_ceiling: {build: xhigh}\n")
    choice = select_effort("build", cfg, _OPUS, escalation_steps=3)
    assert choice.level == "xhigh"
    assert choice.held == (
        "effort held at xhigh for build (would have been max; "
        "ceiling from .sdlc-model-routing.yaml)"
    )


def test_haiku_stage_reports_no_hold() -> None:
    choice = select_effort("merge", BALANCED, "claude-haiku-4-5", escalation_steps=4)
    assert choice.level is None and choice.held == ""


def test_model_escalation_is_unchanged() -> None:
    # The ceiling bounds effort only; the model still climbs Haiku → Sonnet → Opus.
    assert escalate_model("haiku", 1) == "sonnet"
    assert escalate_model("sonnet", 1) == "opus"


# --- YAML override ----------------------------------------------------------


def test_yaml_overrides_a_stage_ceiling() -> None:
    cfg = _cfg("  effort_ceiling: {review: max}\n")
    assert cfg.effort_ceilings["review"] == "max"
    assert cfg.effort_ceilings["build"] == "high"  # additive
    assert select_effort("review", cfg, _OPUS, escalation_steps=4).level == "max"


def test_ceiling_below_base_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"effort_ceiling.*review.*below"):
        _cfg("  effort_ceiling: {review: high}\n")


def test_ceiling_below_overridden_base_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"effort_ceiling.*build.*below"):
        _cfg("  effort: {build: max}\n  effort_ceiling: {build: xhigh}\n")


def test_overriding_base_above_default_ceiling_lifts_the_default() -> None:
    cfg = _cfg("  effort: {review: max}\n")
    assert cfg.effort_ceilings["review"] == "max"
    assert select_effort("review", cfg, _OPUS).level == "max"


@pytest.mark.parametrize("bad", ["{review: turbo}", "[review]", "{review: 3}"])
def test_unknown_level_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="effort_ceiling"):
        _cfg(f"  effort_ceiling: {bad}\n")


# --- banner / status --------------------------------------------------------


def test_banner_shows_base_to_ceiling_per_stage() -> None:
    banner = "\n".join(routing_banner(routing_snapshot(BALANCED)))
    assert "review xhigh→xhigh" in banner
    assert "build high→high" in banner
    assert "bugfix high→xhigh" in banner
    assert "merge medium→medium" in banner


def test_banner_reflects_yaml_ceiling() -> None:
    banner = "\n".join(routing_banner(routing_snapshot(_cfg("  effort_ceiling: {review: max}\n"))))
    assert "review xhigh→max" in banner


def test_banner_has_no_effort_line_without_efforts() -> None:
    banner = "\n".join(routing_banner(routing_snapshot(QUALITY_FIRST)))
    assert "base→ceiling" not in banner


def test_status_markdown_shows_base_to_ceiling() -> None:
    text = "\n".join(format_routing(routing_snapshot(BALANCED)))
    assert "review xhigh→xhigh" in text


def test_snapshot_round_trips_ceilings_and_sources() -> None:
    cfg = _cfg("  effort_ceiling: {review: max}\n")
    back = config_from_snapshot(routing_snapshot(cfg))
    assert back is not None
    assert back.effort_ceilings == cfg.effort_ceilings
    assert select_effort("review", back, _OPUS, escalation_steps=2).level == "max"
    held = select_effort("build", back, _OPUS, escalation_steps=2).held
    assert held.endswith("ceiling from default)")


def test_legacy_snapshot_without_ceilings_keeps_uncapped_climb() -> None:
    snap = routing_snapshot(BALANCED)
    snap.pop("effort_ceilings")
    snap.pop("effort_ceiling_overrides", None)
    cfg = config_from_snapshot(snap)
    assert cfg is not None and cfg.effort_ceilings == {}
    assert select_effort("build", cfg, _OPUS, escalation_steps=3).level == "max"


# --- dispatch + event (integration) ----------------------------------------

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
_FIXED = {"failure_category": "TEST_BUG", "fix_status": "FIXED",
          "tests_passing": True, "bugs_fixed": 1, "tests_fixed": 0}


class _FailBuildTwice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._fails = 2

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append((agent_type, kwargs.get("model"), kwargs.get("effort")))
        if agent_type == "build" and self._fails:
            self._fails -= 1
            return AgentResult(
                agent_type="build",
                data={"branch_name": "feature/x", "build_status": "FAILED",
                      "error_summary": "boom"},
                raw="",
            )
        if agent_type == "bugfix":
            return AgentResult(agent_type="bugfix", data=_FIXED, raw="")
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")


def test_retry_holds_build_effort_at_ceiling_and_logs_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    disp = _FailBuildTwice()
    opts = BuildOptions(
        scope="epic-34", skip_preflight=True, sequential=True, model_profile="balanced"
    )
    story = Story(
        id="34.5-003", title="t", epic_id="epic-34", epic_name="e",
        epic_file="f.md", priority="Should", points=1, agent_type="python",
    )
    run_build(
        opts, queue=[story], ledger=Ledger(tmp_path / "ledger.db"), dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    assert [e for (a, _m, e) in disp.calls if a == "build"] == ["high", "high", "high"]
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        events = [m for (m,) in conn.execute("SELECT message FROM events")]
    assert any(
        "effort held at high for build (would have been xhigh; ceiling from default)" in m
        for m in events
    )
