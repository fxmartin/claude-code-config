# ABOUTME: Tests for Story 23.2-003 — merge the MR + close the story issue + branch cleanup.
# ABOUTME: Host-neutral merge prompt, merge-sha capture into the ledger, GitHub-unchanged regression.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    BuildResult,
    Ledger,
    _extract_merge_sha,
    _record_merge_landing,
    render_merge_prompt,
    run_build,
)
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.issue_host import GITHUB_CR_TERMS, GITLAB_CR_TERMS


def _story(sid: str = "23.2-003") -> Story:
    return Story(
        id=sid,
        title="Land the story change",
        epic_id="epic-23",
        epic_name="pipeline-on-gitlab",
        epic_file="docs/stories/epic-23-pipeline-on-gitlab.md",
        priority="Should",
        points=3,
        agent_type="merge",
    )


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC1: the merge stage is host-neutral — GitHub byte-identical, GitLab says MR
# ---------------------------------------------------------------------------

def test_render_merge_prompt_github_unchanged_by_default() -> None:
    """The default (GitHub) merge prompt is byte-identical to the pre-23.2-003 text."""
    story = _story()
    prompt = render_merge_prompt(story, 7)
    assert prompt.startswith(f"Merge the PR for story {story.id}: {story.title} (PR #7).")
    assert "MR" not in prompt  # no GitLab leakage on the GitHub path


def test_render_merge_prompt_github_explicit_terms_match_default() -> None:
    story = _story()
    assert render_merge_prompt(story, 7) == render_merge_prompt(
        story, 7, cr_terms=GITHUB_CR_TERMS
    )


def test_render_merge_prompt_gitlab_uses_mr_noun() -> None:
    """On a GitLab target the merge prompt names the Merge Request, not a PR (AC1)."""
    story = _story()
    prompt = render_merge_prompt(story, 7, cr_terms=GITLAB_CR_TERMS)
    assert prompt.startswith(
        f"Merge the MR (`glab mr merge`) for story {story.id}: {story.title} (MR #7)."
    )
    assert "Merge the PR" not in prompt
    assert "(PR #7)" not in prompt


# ---------------------------------------------------------------------------
# AC3: the merge sha is captured into the ledger as the story lands DONE
# ---------------------------------------------------------------------------

def test_stories_table_has_merge_sha_column(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    assert "merge_sha" in _columns(db, "stories")


def test_set_and_get_story_merge_sha_roundtrip(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-23", "serial")
    ledger.story_upsert(
        run_id, "23.2-003", "epic-23", "Merge the MR", "Should", 3,
        "merge", "feature/23.2-003", None, "IN_PROGRESS",
    )
    assert ledger.story_merge_sha(run_id, "23.2-003") is None
    ledger.set_story_merge_sha(run_id, "23.2-003", "cafef00d")
    assert ledger.story_merge_sha(run_id, "23.2-003") == "cafef00d"


def test_merge_sha_migration_on_stale_ledger(tmp_path) -> None:
    """A ledger predating migration 10 gains the merge_sha column on migrate."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
        "  completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT NOT NULL);"
        "CREATE TABLE stories (run_id TEXT, story_id TEXT, epic_id TEXT, title TEXT, "
        "  priority TEXT, points INTEGER, agent_type TEXT, branch TEXT, "
        "  pr_number INTEGER, current_stage TEXT, status TEXT NOT NULL, "
        "  PRIMARY KEY(run_id, story_id));"
        "CREATE TABLE stages (run_id TEXT, story_id TEXT, stage_name TEXT, "
        "  attempt INTEGER DEFAULT 1, status TEXT NOT NULL, "
        "  PRIMARY KEY(run_id, story_id, stage_name, attempt));"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
        "  story_id TEXT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, "
        "  source TEXT, message TEXT NOT NULL);"
        "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.commit()
    conn.close()
    assert "merge_sha" not in _columns(db, "stories")
    Ledger(db).ensure_migrated()
    assert "merge_sha" in _columns(db, "stories")


def test_extract_merge_sha_from_result() -> None:
    res = AgentResult(
        agent_type="merge",
        data={"merge_status": "MERGED", "merge_sha": "cafef00d", "merged_at": "x"},
        raw="",
    )
    assert _extract_merge_sha(res) == "cafef00d"


def test_extract_merge_sha_none_when_absent_or_blank() -> None:
    assert _extract_merge_sha(None) is None
    blank = AgentResult(
        agent_type="merge",
        data={"merge_status": "MERGED", "merge_sha": ""},
        raw="",
    )
    assert _extract_merge_sha(blank) is None


def _seed_story_row(ledger: Ledger) -> str:
    """Insert an IN_PROGRESS story row and return its run_id (shared test scaffold)."""
    run_id = ledger.run_create("epic-23", "serial")
    ledger.story_upsert(
        run_id, "23.2-003", "epic-23", "Merge the MR", "Should", 3,
        "merge", "feature/23.2-003", None, "IN_PROGRESS",
    )
    return run_id


def test_record_merge_landing_noop_on_merge_stage_without_landed_sha(tmp_path) -> None:
    """A merge stage that did NOT land (FAILED/empty sha) records nothing (AC3 guard).

    Exercises the defensive ``if not sha: return`` path the happy-path
    ``FakeDispatcher`` never hits — the merge agent always returns a real sha
    there, so this branch needs a direct unit test. The story row must stay
    merge_sha=NULL and no "merge landed" event may be written.
    """
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = _seed_story_row(ledger)

    failed = AgentResult(
        agent_type="merge",
        data={"merge_status": "FAILED", "merge_sha": ""},
        raw="",
    )
    _record_merge_landing("merge", failed, ledger, run_id, _story(), 7)

    assert ledger.story_merge_sha(run_id, "23.2-003") is None
    conn = sqlite3.connect(db)
    try:
        events = conn.execute(
            "SELECT message FROM events WHERE run_id = ? AND story_id = '23.2-003'",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    assert not any("merge landed" in row[0] for row in events)


def test_record_merge_landing_noop_on_non_merge_stage(tmp_path) -> None:
    """A non-merge stage is a no-op even when its result carries a sha-like field.

    The early ``if stage != "merge": return`` must skip the ledger write so a
    coverage/review stage can never stamp a merge sha onto the story row.
    """
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = _seed_story_row(ledger)

    # A coverage result that happens to carry a merge_sha must still be ignored.
    coverage = AgentResult(
        agent_type="coverage",
        data={"coverage_status": "PASS", "merge_sha": "cafef00d"},
        raw="",
    )
    _record_merge_landing("coverage", coverage, ledger, run_id, _story(), 7)

    assert ledger.story_merge_sha(run_id, "23.2-003") is None


def test_record_merge_landing_stamps_sha_and_logs_event(tmp_path) -> None:
    """A landed merge stamps the sha and writes one "merge landed" event (AC3 happy path)."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = _seed_story_row(ledger)

    merged = AgentResult(
        agent_type="merge",
        data={"merge_status": "MERGED", "merge_sha": "cafef00d", "merged_at": "x"},
        raw="",
    )
    _record_merge_landing("merge", merged, ledger, run_id, _story(), 7)

    assert ledger.story_merge_sha(run_id, "23.2-003") == "cafef00d"
    conn = sqlite3.connect(db)
    try:
        events = conn.execute(
            "SELECT message FROM events WHERE run_id = ? AND story_id = '23.2-003'",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    landings = [row[0] for row in events if "merge landed" in row[0]]
    assert landings == ["merge landed: story DONE at cafef00d (cr=#7)"]


def test_run_build_records_merge_sha_when_story_lands(tmp_path) -> None:
    """AC3: a merged story is DONE and carries the merge sha the merge agent reported."""
    from test_build import FakeDispatcher, _sample_queue

    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=ledger,
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert isinstance(result, BuildResult)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT status, merge_sha FROM stories").fetchall()
    finally:
        conn.close()
    assert rows
    for status, merge_sha in rows:
        assert status == "DONE"
        assert merge_sha == "cafef00d"  # the FakeDispatcher merge response sha
