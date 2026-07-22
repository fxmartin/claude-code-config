# ABOUTME: Regression tests for per-attempt model recording (Story 28.1-002) — every
# ABOUTME: dispatched stage row lands a non-NULL `stages.model` on a fresh run.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sdlc.build as build_mod
from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.harness import HarnessConfig
from sdlc.progress import dominant_model, model_of

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
    "bugfix": {"failure_category": "TEST_BUG", "fix_status": "FIXED",
               "tests_passing": True, "bugs_fixed": 1, "tests_fixed": 0},
}

# What the Claude CLI reports it actually ran on, via the result envelope's
# `modelUsage` — a full model id, never the routing tier alias.
_OBSERVED = "claude-opus-4-8"


def _story(points: int = 1) -> Story:
    return Story(
        id="28.1-002", title="t", epic_id="epic-28", epic_name="e",
        epic_file="f.md", priority="Must", points=points, agent_type="python",
    )


class _ObservingDispatcher:
    """Returns a canned success carrying the model the session actually ran on.

    Mirrors the real Claude parser, which reads the model out of the result
    envelope's ``modelUsage`` — so the ledger records observed fact, not the
    pre-dispatch prediction. ``fail_first`` drives one stage into the bugfix loop
    so the recovery rows are exercised too.
    """

    def __init__(self, model: str | None = _OBSERVED, fail_first: str | None = None) -> None:
        self.model = model
        self.fail_first = fail_first
        self._seen = 0

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        if agent_type == self.fail_first:
            self._seen += 1
            if self._seen == 1:
                return AgentResult(
                    agent_type=agent_type,
                    data={"branch_name": "feature/x", "build_status": "FAILED",
                          "error_summary": "boom"},
                    raw="", model=self.model,
                )
        return AgentResult(
            agent_type=agent_type, data=_PAYLOADS[agent_type], raw="", model=self.model,
        )


def _stage_models(db: Path) -> dict[tuple[str, int], str | None]:
    conn = sqlite3.connect(db)
    try:
        return {
            (row[0], row[1]): row[2]
            for row in conn.execute(
                "SELECT stage_name, attempt, model FROM stages WHERE status != 'SKIPPED'"
            ).fetchall()
        }
    finally:
        conn.close()


def _run(opts: BuildOptions, tmp_path: Path, dispatcher) -> Path:
    db = tmp_path / "ledger.db"
    run_build(
        opts, queue=[_story()], ledger=Ledger(db), dispatcher=dispatcher,
        preflight=lambda: True, root=tmp_path,
    )
    return db


# ---------------------------------------------------------------------------
# AC1: every dispatched stage row records a non-NULL model on a fresh run
# ---------------------------------------------------------------------------


def test_every_primary_stage_records_the_observed_model(tmp_path) -> None:
    """Routing off (the default) used to leave `model` NULL on all four stages."""
    db = _run(
        BuildOptions(scope="epic-28", skip_preflight=True, sequential=True),
        tmp_path, _ObservingDispatcher(),
    )
    models = _stage_models(db)
    for stage in ("build", "coverage", "review", "merge"):
        assert models[(stage, 1)] == _OBSERVED, f"{stage} recorded {models.get((stage, 1))}"


def test_recovery_rows_record_the_observed_model(tmp_path, monkeypatch) -> None:
    """The bugfix recovery row is a dispatched stage too — it must not read NULL."""
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    db = _run(
        BuildOptions(scope="epic-28", skip_preflight=True, sequential=True),
        tmp_path, _ObservingDispatcher(fail_first="build"),
    )
    models = _stage_models(db)
    bugfix = [m for (stage, _), m in models.items() if stage == "bugfix"]
    assert bugfix, "bugfix agent was never dispatched"
    assert all(m == _OBSERVED for m in bugfix), bugfix
    # The FAILED first build attempt carries usage too, so it is attributed.
    assert models[("build", 1)] == _OBSERVED


def test_no_stage_row_is_null_on_a_fresh_run(tmp_path, monkeypatch) -> None:
    """The whole point: zero NULL `model` values across a completed fresh run."""
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    db = _run(
        BuildOptions(scope="epic-28", skip_preflight=True, sequential=True,
                     model_profile="balanced"),
        tmp_path, _ObservingDispatcher(fail_first="build"),
    )
    nulls = [key for key, model in _stage_models(db).items() if model is None]
    assert nulls == []


# ---------------------------------------------------------------------------
# AC4: the recorded model is what actually ran, not the routed placeholder
# ---------------------------------------------------------------------------


def test_observed_model_overwrites_the_pre_dispatch_routing_alias(tmp_path) -> None:
    """`stage_start` writes the routed alias; the verified id replaces it."""
    db = _run(
        BuildOptions(scope="epic-28", skip_preflight=True, sequential=True,
                     model_overrides={"build": "opus"}),
        tmp_path, _ObservingDispatcher(),
    )
    assert _stage_models(db)[("build", 1)] == _OBSERVED


def test_registry_harness_model_survives_a_telemetry_free_result(
    tmp_path, monkeypatch
) -> None:
    """A harness with no usage telemetry keeps its registry-resolved model.

    `codex`-style harnesses parse to an AgentResult with no model (and no usage),
    so nothing must overwrite the model `stage_start` resolved from the registry.
    """
    harness = HarnessConfig(
        name="acme", command="acme run --model {model}", parser="codex-exec",
        models={"default": "acme-base", "build": "acme-pro"}, source="registry",
    )
    monkeypatch.setattr(
        build_mod, "resolve_harness", lambda name=None, config_path=None: harness
    )
    db = _run(
        BuildOptions(
            scope="epic-28", skip_preflight=True, sequential=True,
            harness_map={"build": "acme"},
        ),
        tmp_path, _ObservingDispatcher(model=None),
    )
    assert _stage_models(db)[("build", 1)] == "acme-pro"


# ---------------------------------------------------------------------------
# The stream-json helpers the recording is built on
# ---------------------------------------------------------------------------


def test_dominant_model_picks_the_costliest_entry() -> None:
    usage = {
        "claude-haiku-4-5": {"costUSD": 0.01, "outputTokens": 500},
        "claude-opus-4-8": {"costUSD": 11.5, "outputTokens": 74189},
    }
    assert dominant_model(usage) == "claude-opus-4-8"


def test_dominant_model_falls_back_to_output_tokens_without_cost() -> None:
    usage = {
        "a-model": {"outputTokens": 10},
        "b-model": {"outputTokens": 900},
    }
    assert dominant_model(usage) == "b-model"


@pytest.mark.parametrize("value", [None, {}, "nope", {"m": "not-a-dict"}])
def test_dominant_model_none_on_junk(value) -> None:
    assert dominant_model(value) is None


def test_dominant_model_is_deterministic_on_a_tie() -> None:
    usage = {"z-model": {"costUSD": 1.0}, "a-model": {"costUSD": 1.0}}
    assert dominant_model(usage) == "a-model"


def test_model_of_reads_the_assistant_turn_model() -> None:
    assert model_of({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) == (
        "claude-sonnet-5"
    )


@pytest.mark.parametrize(
    "event",
    [None, "text", {"type": "system"}, {"message": {}}, {"message": {"model": ""}}],
)
def test_model_of_none_without_a_model(event) -> None:
    assert model_of(event) is None
