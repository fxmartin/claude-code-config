# ABOUTME: Tests routing-on-by-default + the run's frozen routing snapshot (Story 28.4-001).
# ABOUTME: Covers the default flip, the banner, resolve-and-freeze on resume, and doctor.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import sdlc.build as build_mod
from sdlc.build import BuildOptions, Ledger, run_build, status_snapshot
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.doctor import check_model_routing
from sdlc.model_routing import (
    BALANCED,
    HAIKU,
    OPUS,
    SONNET,
    config_from_snapshot,
    is_routing_off,
    routing_banner,
    routing_snapshot,
)
from sdlc.resume import ROUTING_OFF_SNAPSHOT, run_resume
from sdlc.status import format_routing

_PAYLOADS = {
    "build": {"branch_name": "feature/x", "build_status": "SUCCESS", "commit_sha": "a"},
    "coverage": {
        "pr_number": 100, "pr_url": "u", "coverage_pct": 95.0, "tests_added": 1,
        "coverage_status": "PASS", "security_status": "PASS",
    },
    "review": {"pr_number": 100, "approval_status": "APPROVED", "change_count": 0,
               "final_status": "APPROVED"},
    "merge": {"pr_number": 100, "merge_status": "MERGED", "merge_sha": "b",
              "merged_at": "2026-07-22T00:00:00Z"},
}


class _ModelRecordingDispatcher:
    """Records (stage → model) for each dispatch and returns a canned success."""

    def __init__(self) -> None:
        self.models: dict[str, str | None] = {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.models[agent_type] = kwargs.get("model")
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")


def _story(points: int = 1, sid: str = "28.4-001") -> Story:
    return Story(
        id=sid, title="t", epic_id="epic-28", epic_name="e",
        epic_file="f.md", priority="Must", points=points, agent_type="python",
    )


def _run(opts: BuildOptions, tmp_path, story: Story | None = None):
    disp = _ModelRecordingDispatcher()
    ledger = Ledger(tmp_path / "ledger.db")
    run_build(
        opts,
        queue=[story or _story()],
        ledger=ledger,
        dispatcher=disp,
        preflight=lambda: True,
        root=tmp_path,  # hermetic: keep the git-landed probe off the real repo
    )
    return disp, ledger


# ---------------------------------------------------------------------------
# AC1 — Balanced is the effective default when no profile is configured
# ---------------------------------------------------------------------------


def test_unset_profile_dispatches_the_balanced_map(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    disp, _ = _run(opts, tmp_path)
    assert disp.models == {
        "build": SONNET, "coverage": SONNET, "review": SONNET, "merge": HAIKU,
    }


def test_unset_profile_freezes_a_balanced_snapshot_on_the_run_row(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    _, ledger = _run(opts, tmp_path)
    rid = ledger.latest_run_id()
    snapshot = ledger.run_routing(rid)
    assert snapshot["profile"] == "balanced"
    assert snapshot["stage_models"]["merge"] == HAIKU
    assert snapshot["points_threshold"] == BALANCED.points_threshold
    assert snapshot["escalation_model"] == OPUS
    assert sorted(snapshot["escalatable_stages"]) == ["adversarial", "build", "review"]


def test_banner_is_printed_and_ledger_logged(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    _, ledger = _run(opts, tmp_path)
    printed = capsys.readouterr().err
    assert "model routing: profile=balanced" in printed

    rid = ledger.latest_run_id()
    events = _routing_events(ledger, rid)
    joined = "\n".join(m for _, m in events)
    assert "model routing: profile=balanced" in joined
    assert "merge=haiku" in joined                       # the per-stage map
    assert "escalation → opus" in joined                 # the thresholds in effect
    assert "points >= 8" in joined
    assert all(level == "info" for level, _ in events)


def _routing_events(ledger: Ledger, run_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT level, message FROM events WHERE run_id = ? AND source = 'routing' "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
    return [(r["level"], r["message"]) for r in rows]


# ---------------------------------------------------------------------------
# AC2 — explicit off is loud, and only an explicit off disables routing
# ---------------------------------------------------------------------------


def test_explicit_off_dispatches_no_model_and_says_so_loudly(
    tmp_path, capsys
) -> None:
    opts = BuildOptions(
        scope="epic-28", skip_preflight=True, sequential=True, model_profile="off",
    )
    disp, ledger = _run(opts, tmp_path)
    assert set(disp.models.values()) == {None}

    assert "MODEL ROUTING OFF" in capsys.readouterr().err
    events = _routing_events(ledger, ledger.latest_run_id())
    assert any("MODEL ROUTING OFF" in m for _, m in events)
    # Off is expensive, so it must not read as routine info in the ledger.
    assert all(level == "warn" for level, _ in events)


def test_off_run_freezes_an_explicit_off_snapshot(tmp_path) -> None:
    """Off is persisted as a stated value, never as an absent key."""
    opts = BuildOptions(
        scope="epic-28", skip_preflight=True, sequential=True, model_profile="off",
    )
    _, ledger = _run(opts, tmp_path)
    assert ledger.run_routing(ledger.latest_run_id())["profile"] == "off"


def test_off_on_an_unattended_looking_run_warns_in_the_preflight(
    tmp_path, capsys
) -> None:
    """A --budget ceiling means cost was a concern — routing-off there is called out."""
    opts = BuildOptions(
        scope="epic-28", skip_preflight=True, sequential=True,
        model_profile="off", budget=1_000_000,
    )
    _run(opts, tmp_path)
    err = capsys.readouterr().err
    assert "WARNING: routing is off on an unattended-looking run" in err


# ---------------------------------------------------------------------------
# AC3 — override precedence is unchanged, and the banner shows the effective map
# ---------------------------------------------------------------------------


def test_per_stage_override_keeps_precedence_and_shows_in_the_banner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(
        scope="epic-28", skip_preflight=True, sequential=True,
        model_overrides={"merge": OPUS},
    )
    disp, ledger = _run(opts, tmp_path)
    assert disp.models["merge"] == OPUS          # override beats the HAIKU default
    assert disp.models["build"] == SONNET        # other stages still from the map

    snapshot = ledger.run_routing(ledger.latest_run_id())
    # The snapshot states the *effective* map, i.e. after the override.
    assert snapshot["stage_models"]["merge"] == OPUS
    assert snapshot["overrides"] == {"merge": OPUS}
    joined = "\n".join(m for _, m in _routing_events(ledger, ledger.latest_run_id()))
    assert "explicit --model overrides win: merge=opus" in joined


def test_agent_cmd_override_is_named_in_the_banner(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    monkeypatch.setenv("SDLC_AGENT_CMD", "my-agent -p")
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    _, ledger = _run(opts, tmp_path)
    assert "SDLC_AGENT_CMD overrides the whole agent command" in capsys.readouterr().err
    assert ledger.run_routing(ledger.latest_run_id())["agent_cmd"] == "my-agent -p"


# ---------------------------------------------------------------------------
# AC4 — the governing routing is visible in `sdlc status` and the dashboard
# ---------------------------------------------------------------------------


def test_status_snapshot_carries_the_governing_routing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    _, ledger = _run(opts, tmp_path)
    snap = status_snapshot(ledger)
    assert snap["run"]["routing"]["profile"] == "balanced"
    # Story 28.1-002 owns the per-stage model column; this story surfaces it.
    assert "models" in snap["stories"][0]


def test_format_routing_states_profile_map_and_thresholds() -> None:
    lines = format_routing(routing_snapshot(BALANCED))
    text = "\n".join(lines)
    assert "**Model routing: `balanced`**" in text
    assert "merge=haiku" in text
    assert "Escalates to opus on high-risk or points ≥ 8" in text


def test_format_routing_names_the_explicit_overrides() -> None:
    lines = format_routing(routing_snapshot(BALANCED, overrides={"merge": OPUS}))
    assert "Explicit `--model` overrides applied: merge=opus." in lines


def test_blank_override_profile_is_rejected() -> None:
    """An override must *name* a profile: blank no longer silently means Balanced."""
    from sdlc.model_routing import load_routing_config

    with pytest.raises(ValueError, match="must name a profile"):
        load_routing_config("balanced", override_text='model_routing:\n  profile: ""\n')


def test_format_routing_is_loud_when_off() -> None:
    assert "**MODEL ROUTING OFF**" in "\n".join(format_routing(routing_snapshot(None)))


def test_format_routing_is_silent_for_a_run_with_no_snapshot() -> None:
    """A pre-28.4-001 run renders as it always did, never claiming a routing."""
    assert format_routing({}) == []


# ---------------------------------------------------------------------------
# AC5 — doctor warns on routing-off, and fails when it looks unattended
# ---------------------------------------------------------------------------


def _seed_run(db: Path, *, routing: dict | None, stories: int, budget: int = 0) -> str:
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-28", "serial")
    ledger.set_total(rid, stories)
    ledger.event_log(rid, "", "info", "config", json.dumps({"budget": budget}))
    if routing is not None:
        ledger.run_set_routing(rid, routing)
    return rid


def test_doctor_is_clean_when_routing_is_engaged(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    _seed_run(db, routing=routing_snapshot(BALANCED), stories=3)
    finding = check_model_routing(db)
    assert finding.status == "CLEAN"
    assert "balanced" in finding.detail


def test_doctor_warns_when_routing_is_off_on_an_attended_run(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    _seed_run(db, routing=routing_snapshot(None), stories=1)
    finding = check_model_routing(db)
    assert finding.status == "WARN"
    assert "MODEL ROUTING OFF" in finding.detail


def test_doctor_fails_when_routing_is_off_on_a_batch(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    _seed_run(db, routing=routing_snapshot(None), stories=4)
    finding = check_model_routing(db)
    assert finding.status == "FAIL"
    assert "batch of 4 stories" in finding.detail


def test_doctor_fails_when_routing_is_off_with_a_budget(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    _seed_run(db, routing=routing_snapshot(None), stories=1, budget=500_000)
    finding = check_model_routing(db)
    assert finding.status == "FAIL"
    assert "--budget=500000" in finding.detail


def test_doctor_is_clean_without_a_ledger(tmp_path) -> None:
    assert check_model_routing(tmp_path / "absent.db").status == "CLEAN"


# ---------------------------------------------------------------------------
# AC6/AC7 — resolve-and-freeze: a resume replays the snapshot, never re-resolves
# ---------------------------------------------------------------------------

_SAMPLE_EPIC = """# Epic 28

##### Story 28.9-001: One
**Priority**: Must
**Points**: 1
**Dependencies**: None.
"""


def _make_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-28-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def _seed_interrupted_at_review(db: Path, *, routing: dict | None) -> str:
    """One story with build DONE and review interrupted — resume owes review."""
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-28", "serial")
    ledger.set_total(rid, 1)
    ledger.event_log(
        rid, "", "info", "config", json.dumps({"skip_coverage": True, "model_profile": ""})
    )
    ledger.story_upsert(
        rid, "28.9-001", "28", "One", "Must", 1, "general-purpose", "", None, "TODO"
    )
    ledger.stage_start(rid, "28.9-001", "build", 1)
    ledger.stage_finish(rid, "28.9-001", "build", 1, "DONE")
    ledger.set_story_pr(rid, "28.9-001", 100)
    ledger.stage_start(rid, "28.9-001", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(rid, "28.9-001", "IN_PROGRESS")
    if routing is not None:
        ledger.run_set_routing(rid, routing)
    return rid


def _resume(tmp_path: Path, db: Path) -> _ModelRecordingDispatcher:
    disp = _ModelRecordingDispatcher()
    run_resume("epic-28", ledger=Ledger(db), dispatcher=disp, root=tmp_path)
    return disp


def test_resume_replays_the_frozen_snapshot_over_a_changed_config(
    tmp_path, monkeypatch
) -> None:
    """AC7: an edit between a run and its resume can never alter that run's routing."""
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    _make_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_interrupted_at_review(db, routing=routing_snapshot(BALANCED))

    # The config file and an override both change *after* the run was created.
    monkeypatch.chdir(tmp_path)
    Path(".sdlc-model-routing.yaml").write_text(
        "model_routing:\n  profile: quality-first\n", encoding="utf-8"
    )
    disp = _resume(tmp_path, db)
    # Still the frozen Balanced map, not the quality-first (all-Opus) edit.
    assert disp.models["review"] == SONNET


def test_pre_change_run_resumes_routing_off_not_balanced(tmp_path, monkeypatch) -> None:
    """AC8: a run created before the flip keeps its original routing-off behaviour.

    Its persisted ``model_profile`` is "" — which used to mean off and now means
    Balanced — so only the frozen snapshot can tell the two apart.
    """
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    _make_project(tmp_path)
    db = tmp_path / "ledger.db"
    # `routing=None` = the row Migration 15's backfill targets.
    _seed_interrupted_at_review(db, routing=None)
    disp = _resume(tmp_path, db)
    assert disp.models["review"] is None  # CLI default, exactly as it originally ran


def test_migration_stamps_legacy_run_rows_as_routing_off(tmp_path) -> None:
    """The versioned backfill freezes every pre-flip run at routing-off."""
    db = tmp_path / "ledger.db"
    # A pre-flip ledger: its `runs` table has no model_routing column at all, and
    # it carries a run whose routing was therefore never stated anywhere.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE runs ("
            "id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
            "finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
            "completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
            "status TEXT NOT NULL, actor TEXT)"
        )
        conn.execute(
            "INSERT INTO runs(id, scope, mode, status, started_at) "
            "VALUES ('legacy', 'epic-28', 'serial', 'IN_PROGRESS', CURRENT_TIMESTAMP)"
        )
    assert Ledger(db).run_routing("legacy") == {}

    Ledger(db).ensure_migrated()

    snapshot = Ledger(db).run_routing("legacy")
    assert snapshot["profile"] == "off"
    assert snapshot["legacy"] is True
    assert is_routing_off(snapshot)


def test_migration_never_overwrites_a_snapshot_it_already_stamped(tmp_path) -> None:
    """Idempotent: re-running migrations leaves an existing snapshot untouched."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-28", "serial")
    ledger.run_set_routing(rid, routing_snapshot(BALANCED))
    ledger.ensure_migrated()
    assert ledger.run_routing(rid)["profile"] == "balanced"


# ---------------------------------------------------------------------------
# Snapshot round-trip (the resolve-and-freeze primitive)
# ---------------------------------------------------------------------------


def test_snapshot_round_trips_to_an_equivalent_config() -> None:
    snapshot = routing_snapshot(BALANCED)
    assert config_from_snapshot(snapshot) == BALANCED


def test_snapshot_round_trip_survives_json(tmp_path) -> None:
    """The snapshot lives in a TEXT column, so JSON must not lose the map."""
    snapshot = json.loads(json.dumps(routing_snapshot(BALANCED, overrides={"merge": OPUS})))
    config = config_from_snapshot(snapshot)
    assert config is not None
    assert config.stage_models["merge"] == OPUS


def test_off_snapshot_rebuilds_as_no_config() -> None:
    assert config_from_snapshot(routing_snapshot(None)) is None
    assert config_from_snapshot({}) is None
    assert config_from_snapshot(None) is None


def test_resume_off_snapshot_constant_is_an_off_snapshot() -> None:
    assert is_routing_off(ROUTING_OFF_SNAPSHOT)


def test_banner_lists_pinned_stages_when_a_profile_pins_one() -> None:
    from dataclasses import replace

    pinned = replace(BALANCED, pinned_stages=frozenset({"adversarial"}))
    text = "\n".join(routing_banner(routing_snapshot(pinned)))
    assert "pinned to opus: adversarial" in text


@pytest.mark.parametrize("profile", ["off", "none", "OFF", "  None  "])
def test_is_routing_off_recognises_every_opt_out_spelling(profile: str) -> None:
    assert is_routing_off({"profile": profile})


# ---------------------------------------------------------------------------
# Degradation — an unreadable snapshot reads as off, and the banner is optional
# ---------------------------------------------------------------------------


def test_run_routing_is_empty_when_the_ledger_file_is_absent(tmp_path) -> None:
    """No ledger at all is the same "nothing stated" as a pre-flip row."""
    assert Ledger(tmp_path / "absent.db").run_routing("nope") == {}


def _run_row_with_routing(db: Path, raw: str) -> tuple[Ledger, str]:
    """A ledger holding one run whose model_routing column is exactly ``raw``."""
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-28", "serial")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runs SET model_routing = ? WHERE id = ?", (raw, rid))
    return ledger, rid


def test_run_routing_degrades_to_off_on_a_corrupt_snapshot(tmp_path) -> None:
    """Unparseable JSON reads as routing-off, not as a crash mid-resume."""
    ledger, rid = _run_row_with_routing(tmp_path / "ledger.db", "{not json")
    assert ledger.run_routing(rid) == {}
    assert is_routing_off(ledger.run_routing(rid))


def test_run_routing_degrades_to_off_on_a_snapshot_that_is_not_an_object(
    tmp_path,
) -> None:
    """Valid JSON of the wrong shape is rejected rather than handed downstream."""
    ledger, rid = _run_row_with_routing(
        tmp_path / "ledger.db", json.dumps(["balanced"])
    )
    assert ledger.run_routing(rid) == {}
    assert config_from_snapshot(ledger.run_routing(rid)) is None


def test_a_broken_banner_never_fails_an_otherwise_good_build(
    tmp_path, monkeypatch
) -> None:
    """Routing visibility is best-effort: it must not take the run down with it."""
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)

    def _boom(snapshot):
        raise RuntimeError("banner exploded")

    monkeypatch.setattr(build_mod, "routing_banner", _boom)
    opts = BuildOptions(scope="epic-28", skip_preflight=True, sequential=True)
    disp, ledger = _run(opts, tmp_path)

    # Every stage still dispatched on the Balanced map the run froze...
    assert disp.models == {
        "build": SONNET, "coverage": SONNET, "review": SONNET, "merge": HAIKU,
    }
    assert ledger.run_routing(ledger.latest_run_id())["profile"] == "balanced"
    # ...the run simply has no banner to show for it.
    assert _routing_events(ledger, ledger.latest_run_id()) == []
