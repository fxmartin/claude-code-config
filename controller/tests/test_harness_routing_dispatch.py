# ABOUTME: Tests for per-role `--harness` routing wired into actual dispatch (Story 20.7-001).
# ABOUTME: Asserts the build loop dispatches each stage on its mapped harness — not just the ledger label.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import BuildOptions, Ledger, run_build
from sdlc.cohort import Story
from sdlc.harness import resolve_harness
from sdlc.role_routing import PIPELINE_ROLES

# The checked-in registry the real run loads — the same file cli.py validates.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"


def _one_story() -> list[Story]:
    """A single dependency-free story so only the four pipeline stages dispatch."""
    return [Story("h1-001", "Routing story", "99", "sample", "epic-99.md", "P1", 2, "py", [])]


def _payload(agent_type: str, story) -> dict:
    sid = getattr(story, "id", "x")
    return {
        "build": {
            "branch_name": f"feature/{sid}",
            "build_status": "SUCCESS",
            "commit_sha": "deadbeef",
        },
        "coverage": {
            "pr_number": 100,
            "pr_url": "https://example/pull/100",
            "coverage_pct": 95.0,
            "tests_added": 3,
            "coverage_status": "PASS",
            "security_status": "PASS",
        },
        "review": {
            "pr_number": 100,
            "approval_status": "APPROVED",
            "change_count": 0,
            "final_status": "APPROVED",
        },
        "merge": {
            "pr_number": 100,
            "merge_status": "MERGED",
            "merge_sha": "cafef00d",
            "merged_at": "2026-06-12T00:00:00Z",
        },
    }[agent_type]


class RecordingDispatcher:
    """A fake dispatcher that records the argv + parser routed to each stage.

    Unlike ``harness.to_argv()`` in isolation, this captures what the *build
    loop* actually hands the dispatch seam — the real subject of Story 20.7-001.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self, agent_type, prompt, *, story=None, agent_cmd=None, parser=None, model=None, **kwargs
    ):
        from sdlc.dispatch import AgentResult

        self.calls.append(
            {
                "stage": agent_type,
                "agent_cmd": agent_cmd,
                "parser": parser,
                "model": model,
            }
        )
        return AgentResult(agent_type=agent_type, data=_payload(agent_type, story), raw="")

    def by_stage(self) -> dict[str, dict]:
        return {c["stage"]: c for c in self.calls}


def _stage_rows(db: Path) -> dict[str, str]:
    """Map stage_name -> recorded harness column from the ledger."""
    conn = sqlite3.connect(db)
    try:
        return {
            name: harness
            for name, harness in conn.execute(
                "SELECT stage_name, harness FROM stages"
            ).fetchall()
        }
    finally:
        conn.close()


# --- AC1: a full codex map dispatches every stage on codex (zero claude) ----


def test_full_codex_map_dispatches_codex_argv_for_every_stage(tmp_path) -> None:
    codex_argv = resolve_harness("codex", config_path=CONFIG_PATH).to_argv()
    disp = RecordingDispatcher()
    opts = BuildOptions(
        scope="epic-99",
        skip_preflight=True,
        sequential=True,
        harness_map={role: "codex" for role in PIPELINE_ROLES},
    )
    result = run_build(
        opts,
        queue=_one_story(),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.completed == 1

    seen = disp.by_stage()
    assert {"build", "coverage", "review", "merge"}.issubset(seen)
    for stage, call in seen.items():
        assert call["agent_cmd"] == codex_argv, f"{stage} did not route to codex argv"
        assert call["parser"] == "codex-exec", f"{stage} kept the claude parser"
        # The whole point of AC3: zero `claude` processes.
        assert not any("claude" in token for token in call["agent_cmd"])


# --- AC2: a mixed map routes per stage and the ledger matches what ran ------


def test_mixed_map_routes_each_stage_and_ledger_matches(tmp_path) -> None:
    codex_argv = resolve_harness("codex", config_path=CONFIG_PATH).to_argv()
    db = tmp_path / "ledger.db"
    disp = RecordingDispatcher()
    opts = BuildOptions(
        scope="epic-99",
        skip_preflight=True,
        sequential=True,
        harness_map={"build": "claude", "review": "codex"},
    )
    run_build(
        opts,
        queue=_one_story(),
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )

    seen = disp.by_stage()
    # The build stage runs on claude — a claude argv, the stream-json default parser.
    assert any("claude" in token for token in seen["build"]["agent_cmd"])
    assert seen["build"]["parser"] is None
    # The review stage runs on the codex adapter argv + codex-exec parser.
    assert seen["review"]["agent_cmd"] == codex_argv
    assert seen["review"]["parser"] == "codex-exec"
    # An unmapped stage (coverage/merge) falls back to claude.
    assert seen["coverage"]["parser"] is None

    # The ledger harness column matches what actually ran (AC2).
    rows = _stage_rows(db)
    assert rows["build"] == "claude"
    assert rows["review"] == "codex"


# --- AC3: no map and no override → dispatch is byte-identical to today ------


def test_no_harness_map_passes_no_agent_cmd_or_parser(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    disp = RecordingDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_one_story(),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert disp.calls, "expected at least one dispatch"
    for call in disp.calls:
        # The default path passes neither — dispatch resolves its own claude argv
        # and stream-json parser exactly as before this story.
        assert call["agent_cmd"] is None
        assert call["parser"] is None


# --- AC4: codex (parallel:false) under a parallel run degrades, never crashes -


def test_codex_route_survives_a_parallel_run(tmp_path) -> None:
    codex_argv = resolve_harness("codex", config_path=CONFIG_PATH).to_argv()
    disp = RecordingDispatcher()
    # A real parallel run (concurrency>1) routing every role to codex. Codex
    # declares parallel:false / worktree_isolation:false; the build must degrade
    # gracefully (the existing Story 20.5 warn path) and complete, not crash.
    opts = BuildOptions(
        scope="epic-99",
        skip_preflight=True,
        concurrency=3,
        harness_map={role: "codex" for role in PIPELINE_ROLES},
    )
    result = run_build(
        opts,
        queue=_one_story(),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.completed == 1
    assert result.failed == 0
    # The route held under the parallel path — every dispatched stage still codex.
    for call in disp.calls:
        assert call["agent_cmd"] == codex_argv
        assert call["parser"] == "codex-exec"
