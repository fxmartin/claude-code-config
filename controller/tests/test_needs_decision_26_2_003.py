# ABOUTME: Tests for story 26.2-003 — a confirmed finding the story can't fix parks for a decision.
# ABOUTME: needs_decision is accepted, never retried, parks NEEDS_ATTENTION with its reasoning visible.

from __future__ import annotations

from pathlib import Path

import pytest
from test_recovery_push_35_5_001 import (
    _PAYLOADS,
    SID,
    _commit,
    _git,
    _ledger,
    _messages,
    _remote_sha,
    _repo,
    _story,
)

from sdlc import build as build_mod
from sdlc.build import (
    BuildOptions,
    Ledger,
    _run_bugfix_round,
    _surface_finding_dispositions,
    render_bugfix_prompt,
    status_snapshot,
)
from sdlc.dispatch import AgentResult
from sdlc.registry import Registry, RunRecord
from sdlc.scheduler import ledger_decision_park

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_FILES = [
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/_shared/bugfix-agent-prompt.md",
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md",
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md",
]

FINDING = "response cache is never wired into the app"
REASONING = "correct, but the wiring belongs to the composition root, which no story owns"


def _decision_envelope(**over) -> dict:
    data = {
        "failure_category": "CODE_BUG",
        "root_cause": "the cache is built but no composition root registers it",
        "fix_status": "UNFIXED",
        "tests_passing": True,
        "bugs_fixed": 0,
        "tests_fixed": 0,
        "finding_dispositions": [
            {"finding": FINDING, "disposition": "needs_decision", "reasoning": REASONING}
        ],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# AC2 — both prompts list the three dispositions and when needs_decision applies.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        pytest.param(render_bugfix_prompt(_story(), "review", "finding"), id="build.py"),
        *[pytest.param(p.read_text(encoding="utf-8"), id=p.parent.name) for p in _PROMPT_FILES],
    ],
)
def test_prompt_lists_three_dispositions_and_bounds_needs_decision(text: str) -> None:
    for value in ("implemented", "disputed", "needs_decision"):
        assert value in text
    lowered = " ".join(text.lower().split())
    assert "cannot fix" in lowered  # when it applies
    assert "never a way to skip" in lowered  # and what it is not


# ---------------------------------------------------------------------------
# AC3 — the ledger event carries the finding and its reasoning.
# ---------------------------------------------------------------------------

def test_needs_decision_finding_surfaces_with_reasoning(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    _surface_finding_dispositions(ledger, run_id, SID, _decision_envelope())
    [event] = [e for e in ledger.recent_events(run_id, limit=10) if "decision" in e["message"]]
    assert event["level"] == "warn"
    assert FINDING in event["message"] and REASONING in event["message"]


def test_round_with_needs_decision_is_not_a_fix_even_when_reported_fixed(tmp_path) -> None:
    """A FIXED, green round that leaves a finding for FX is still no fix to retry on."""
    ledger, run_id = _ledger(tmp_path)

    def dispatch(agent_type, prompt, story=None, **kw):
        return AgentResult(agent_type, _decision_envelope(fix_status="FIXED"), "")

    out = _run_bugfix_round(_story(), "review", "finding", BuildOptions(), ledger, run_id, dispatch)

    assert out.fixed is False
    assert out.decisions == (f"{FINDING}: {REASONING}",)


def test_round_without_needs_decision_has_no_decisions(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    data = _decision_envelope(finding_dispositions=[
        {"finding": "x", "disposition": "disputed", "reasoning": "guarded"},
    ])

    def dispatch(agent_type, prompt, story=None, **kw):
        return AgentResult(agent_type, data, "")

    out = _run_bugfix_round(_story(), "review", "finding", BuildOptions(), ledger, run_id, dispatch)
    assert out.decisions == ()


# ---------------------------------------------------------------------------
# AC3/AC4 — the stage loop parks NEEDS_ATTENTION at once, pushing any commits.
# ---------------------------------------------------------------------------

def _run_loop(work, tmp_path, monkeypatch, *, commit: bool):
    """Drive run_build: review rejects once; the bugfix answers needs_decision."""
    monkeypatch.chdir(work)
    monkeypatch.setattr(build_mod, "_run_merge_ci_gate", lambda *a, **kw: None)
    seen = {"review": 0, "bugfix": 0}

    def dispatch(agent_type, prompt, story=None, **kw):
        if agent_type == "review":
            seen["review"] += 1
            return AgentResult(agent_type, {
                "pr_number": 100, "approval_status": "CHANGES_NEEDED",
                "change_count": 1, "final_status": "REJECTED",
            }, "")
        if agent_type == "bugfix":
            seen["bugfix"] += 1
            if commit:  # it implemented another finding before parking this one
                _commit(work, "other-finding.txt")
            return AgentResult(agent_type, _decision_envelope(), "")
        return AgentResult(agent_type, _PAYLOADS[agent_type], "")

    ledger = Ledger(tmp_path / "loop.db")
    result = build_mod.run_build(
        BuildOptions(scope="epic-26", skip_preflight=True, sequential=True, auto=True),
        queue=[_story()], ledger=ledger, dispatcher=dispatch, preflight=lambda: True, root=work,
    )
    return result, seen, ledger


def test_needs_decision_parks_needs_attention_with_no_further_attempts(
    tmp_path, monkeypatch
) -> None:
    work = _repo(tmp_path)
    result, seen, ledger = _run_loop(work, tmp_path, monkeypatch, commit=False)

    assert (result.failed, result.needs_attention) == (0, 1)
    assert seen == {"review": 1, "bugfix": 1}  # no retry, no further bugfix round
    [row] = ledger.story_rows(result.run_id)
    assert row["status"] == "NEEDS_ATTENTION"
    messages = _messages(ledger, result.run_id)
    assert not [m for m in messages if "recovery exhausted" in m]
    assert any(FINDING in m and REASONING in m for m in messages)
    detail = status_snapshot(ledger, result.run_id)["stories"][0]["status_detail"]
    assert detail.startswith("needs decision")
    assert FINDING in detail and REASONING in detail


def test_needs_decision_park_pushes_the_other_findings_commits(tmp_path, monkeypatch) -> None:
    work = _repo(tmp_path)
    result, _, ledger = _run_loop(work, tmp_path, monkeypatch, commit=True)

    assert result.needs_attention == 1
    assert _remote_sha(work) == _git(work, "rev-parse", "HEAD")
    detail = status_snapshot(ledger, result.run_id)["stories"][0]["status_detail"]
    assert detail.startswith("needs decision")  # the push does not overwrite the reason


def test_cli_status_prints_the_decision(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    work = _repo(tmp_path)
    result, _, ledger = _run_loop(work, tmp_path, monkeypatch, commit=False)

    out = CliRunner().invoke(app, ["status", "--db", str(ledger.db_path), "--run", result.run_id])
    assert out.exit_code == 0
    assert REASONING in out.output


# ---------------------------------------------------------------------------
# AC3 — the queue job's detail names the finding and reasoning.
# ---------------------------------------------------------------------------

def test_ledger_decision_park_names_each_parked_story(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    assert ledger_decision_park(str(ledger.db_path), run_id) is None
    build_mod._park_for_decision(
        SID, ledger, run_id, (f"{FINDING}: {REASONING}",), workdir=tmp_path,
    )
    ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")

    reason = ledger_decision_park(str(ledger.db_path), run_id)
    assert reason == f"{SID}: needs decision · {FINDING}: {REASONING}"


def test_ledger_decision_park_ignores_other_park_notes_and_bad_ledgers(tmp_path) -> None:
    ledger, run_id = _ledger(tmp_path)
    ledger.event_log(run_id, SID, "info", "controller", "fix pushed · abc1234", kind="park-note")
    ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")
    assert ledger_decision_park(str(ledger.db_path), run_id) is None
    assert ledger_decision_park(str(tmp_path / "missing.db"), run_id) is None


def test_queue_job_parked_on_a_decision_carries_the_reasoning(tmp_path) -> None:
    from test_scheduler import Clock, FakeLauncher, _run, _store
    from test_scheduler import _repo as _queue_repo

    store = _store(tmp_path)
    repo = _queue_repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-26")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=1, code=1)
    clock = Clock()
    ledger, run_id = _ledger(tmp_path)
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            build_mod._park_for_decision(
                SID, ledger, run_id, (f"{FINDING}: {REASONING}",), workdir=tmp_path,
            )
            ledger.set_story_status(run_id, SID, "NEEDS_ATTENTION")
            registry.register(
                RunRecord(run_id=run_id, repo=repo, db=str(ledger.db_path), scope="epic-26",
                          pid=launcher.procs[0].pid, status="NEEDS_ATTENTION",
                          started_at=clock.now.isoformat(), finished_at=clock.now.isoformat())
            )
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
         registry=registry)

    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert FINDING in job.reason and REASONING in job.reason
