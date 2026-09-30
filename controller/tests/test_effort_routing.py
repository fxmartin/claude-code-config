# ABOUTME: Tests the per-stage effort map in model routing (Story 34.4-001).
# ABOUTME: Covers default map, override, unsupported omission, escalation, ledger column, harness drop.

from __future__ import annotations

import sqlite3

import pytest

import sdlc.build as build_mod
from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult, resolve_agent_cmd
from sdlc.harness import HarnessConfig
from sdlc.model_routing import (
    BALANCED,
    QUALITY_FIRST,
    EffortChoice,
    escalate_effort,
    load_routing_config,
    routing_banner,
    routing_snapshot,
    select_effort,
    tier_of,
)

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


def _story() -> Story:
    return Story(
        id="34.4-001", title="t", epic_id="epic-34", epic_name="e",
        epic_file="f.md", priority="Should", points=1, agent_type="python",
    )


class _Dispatcher:
    """Records (agent_type, model, effort) per dispatch; optionally fails build once."""

    def __init__(self, fail_build_once: bool = False) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._fail = fail_build_once

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append((agent_type, kwargs.get("model"), kwargs.get("effort")))
        if agent_type == "build" and self._fail:
            self._fail = False
            return AgentResult(
                agent_type="build",
                data={"branch_name": "feature/x", "build_status": "FAILED",
                      "error_summary": "boom"},
                raw="",
            )
        if agent_type == "bugfix":
            return AgentResult(agent_type="bugfix", data=_FIXED, raw="")
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")

    def effort(self, stage: str) -> str | None:
        return next(e for (a, _m, e) in self.calls if a == stage)


def _run(tmp_path, monkeypatch, disp, **opt_kwargs):
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    opts = BuildOptions(
        scope="epic-34", skip_preflight=True, sequential=True,
        model_profile="balanced", **opt_kwargs,
    )
    ledger = Ledger(tmp_path / "ledger.db")
    run_build(
        opts, queue=[_story()], ledger=ledger, dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    return ledger


def _rows(tmp_path, sql: str):
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        return conn.execute(sql).fetchall()


# --- default map ------------------------------------------------------------


def test_balanced_default_map_matches_spec() -> None:
    assert BALANCED.stage_efforts == {
        "discovery": "low", "docs": "low", "coverage": "medium", "build": "high",
        "review": "xhigh", "merge": "medium", "bugfix": "high",
    }


def test_other_profiles_carry_no_effort_map() -> None:
    assert QUALITY_FIRST.stage_efforts == {}
    assert select_effort("review", QUALITY_FIRST, "opus") == EffortChoice()


def test_select_effort_routing_off_is_empty() -> None:
    assert select_effort("build", None, "opus") == EffortChoice()


def test_balanced_dispatch_argv_carries_effort(tmp_path, monkeypatch) -> None:
    disp = _Dispatcher()
    _run(tmp_path, monkeypatch, disp)
    assert disp.effort("build") == "high"
    assert disp.effort("coverage") == "medium"
    assert disp.effort("review") == "xhigh"


def test_resolve_agent_cmd_adds_effort_flag(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    cmd = resolve_agent_cmd(model="opus", role="build", effort="high")
    assert cmd[-2:] == ["--effort", "high"]
    assert "--effort" not in resolve_agent_cmd(model="opus", role="build")
    assert resolve_agent_cmd(["my-agent"], effort="high") == ["my-agent"]


# --- ledger -----------------------------------------------------------------


def test_ledger_stage_row_records_effort(tmp_path, monkeypatch) -> None:
    _run(tmp_path, monkeypatch, _Dispatcher())
    rows = dict(_rows(tmp_path, "SELECT stage_name, effort FROM stages"))
    assert rows["build"] == "high"
    assert rows["review"] == "xhigh"


# --- override + banner ------------------------------------------------------


def test_override_wins_and_banner_prints_effort() -> None:
    cfg = load_routing_config(
        "balanced",
        override_text="model_routing:\n  stages: {review: opus}\n  effort: {review: max}\n",
    )
    assert cfg is not None
    assert cfg.stage_efforts["review"] == "max"
    assert cfg.stage_efforts["build"] == "high"  # additive
    assert select_effort("review", cfg, "claude-opus-5-5").level == "max"
    banner = "\n".join(routing_banner(routing_snapshot(cfg)))
    assert "review=claude-opus-5-5@max" in banner


@pytest.mark.parametrize("bad", ["{review: turbo}", "[review]", "{review: 3}"])
def test_override_rejects_invalid_effort(bad) -> None:
    with pytest.raises(ValueError, match="effort"):
        load_routing_config("balanced", override_text=f"model_routing:\n  effort: {bad}\n")


def test_snapshot_round_trips_effort() -> None:
    from sdlc.model_routing import config_from_snapshot

    cfg = config_from_snapshot(routing_snapshot(BALANCED))
    assert cfg is not None and cfg.stage_efforts == BALANCED.stage_efforts


# --- unsupported level ------------------------------------------------------


def test_haiku_omits_effort_with_reason() -> None:
    choice = select_effort("merge", BALANCED, "claude-haiku-4-5")
    assert choice.level is None
    assert "merge" in choice.reason and "does not support effort" in choice.reason


def test_unknown_model_omits_effort() -> None:
    assert select_effort("build", BALANCED, "gpt-9").level is None


def test_haiku_stage_dispatch_omits_flag_and_logs_debug(tmp_path, monkeypatch) -> None:
    disp = _Dispatcher()
    _run(tmp_path, monkeypatch, disp)
    assert disp.effort("merge") is None  # Balanced merge runs on Haiku
    assert dict(_rows(tmp_path, "SELECT stage_name, effort FROM stages"))["merge"] is None
    debug = _rows(tmp_path, "SELECT message FROM events WHERE level = 'debug'")
    assert any("effort medium omitted for merge" in m for (m,) in debug)


# --- escalation -------------------------------------------------------------


def test_escalate_effort_climbs_and_caps() -> None:
    assert escalate_effort("high", 1) == "xhigh"
    assert escalate_effort("xhigh", 1) == "max"
    assert escalate_effort("max", 3) == "max"
    assert escalate_effort("low", 0) == "low"
    assert escalate_effort(None, 2) is None


def test_retry_climbs_effort_with_tier_and_event_names_both(tmp_path, monkeypatch) -> None:
    disp = _Dispatcher(fail_build_once=True)
    _run(tmp_path, monkeypatch, disp)
    build_efforts = [e for (a, _m, e) in disp.calls if a == "build"]
    assert build_efforts == ["high", "xhigh"]
    events = [m for (m,) in _rows(tmp_path, "SELECT message FROM events")]
    assert any("escalated to" in m and "model=" in m and "effort=xhigh" in m for m in events)


# --- non-claude harness -----------------------------------------------------


def _harness(caps: dict[str, bool], source: str = "registry") -> HarnessConfig:
    return HarnessConfig(
        name="codex", command="codex exec", parser="codex-exec",
        capabilities=caps, source=source,
    )


def test_registry_harness_argv_ignores_effort() -> None:
    assert "--effort" not in _harness({}).to_argv(model="opus", stage="build", effort="high")


def test_non_claude_harness_drops_effort(tmp_path, monkeypatch) -> None:
    disp = _Dispatcher()
    _run(tmp_path, monkeypatch, disp, harness_map={"build": "codex"})
    assert disp.effort("build") is None
    assert dict(_rows(tmp_path, "SELECT stage_name, effort FROM stages"))["build"] is None
    debug = _rows(tmp_path, "SELECT message FROM events WHERE level = 'debug'")
    assert any("effort high omitted for build" in m and "effort_aware" in m for (m,) in debug)
    # The claude-mapped stages are unaffected.
    assert disp.effort("review") == "xhigh"


def test_agent_cmd_env_drops_effort(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "my-agent")
    choice = build_mod._resolved_stage_effort(
        "build", _story(), BuildOptions(scope="epic-34", model_profile="balanced")
    )
    assert choice.level is None and "SDLC_AGENT_CMD" in choice.reason


def test_tier_of_empty_model_is_none() -> None:
    assert tier_of(None) is None
    assert tier_of("") is None


def test_registry_failure_keeps_effort(tmp_path, monkeypatch) -> None:
    """Best-effort registry resolution: a broken registry must not drop the flag."""

    def _boom(*_a, **_k):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(build_mod, "resolve_harness", _boom)
    choice = build_mod._resolved_stage_effort(
        "build", _story(),
        BuildOptions(scope="epic-34", model_profile="balanced", harness_map={"build": "codex"}),
    )
    assert choice.level == "high"
