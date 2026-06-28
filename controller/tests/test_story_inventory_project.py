# ABOUTME: Tests for the Epic-22 projector that loads MD story specs into the inventory.
# ABOUTME: Story 22.1-002 — full parse, idempotent re-run, added/removed handling, cache preservation.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import Ledger
from sdlc.story_inventory import parse_inventory_specs, project_specs

# A minimal but representative epic, in the exact format the real epics use:
# `##### Story N.F-NNN:` headers + `**Story Points**:` + `**Risk Level**:`. Two
# features (A=22.1, B=22.2) so feature derivation is exercised, and a Risk line
# with a trailing em-dash aside so the first-word capture is exercised.
_SAMPLE_EPIC = """# Epic 22: Sample

##### Story 22.1-001: First story
**User Story**: As FX, I want a thing.
**Priority**: Should Have
**Story Points**: 3

**Risk Level**: Medium
**Dependencies**: None

##### Story 22.1-002: Second story
**User Story**: As FX, I want another thing.
**Priority**: Should Have
**Story Points**: 5

**Risk Level**: High — touches a shared seam; must stay idempotent
**Dependencies**: 22.1-001

##### Story 22.2-001: Third story
**User Story**: As a contributor, I want a third thing.
**Priority**: Should Have
**Story Points**: 2

**Risk Level**: Low
**Dependencies**: None
"""


def _write_epic(root: Path, name: str, body: str) -> None:
    story_dir = root / "docs" / "stories"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / name).write_text(body, encoding="utf-8")


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _row(db: Path, story_id: str) -> dict | None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT * FROM story_inventory WHERE story_id = ?", (story_id,)
        ).fetchone()
        return dict(r) if r is not None else None
    finally:
        conn.close()


def _count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM story_inventory").fetchone()[0]
    finally:
        conn.close()


# --- parsing -----------------------------------------------------------------


def test_parse_extracts_every_story_with_fields(tmp_path: Path) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)

    specs = parse_inventory_specs(tmp_path)
    by_id = {s.story_id: s for s in specs}

    assert set(by_id) == {"22.1-001", "22.1-002", "22.2-001"}

    s = by_id["22.1-002"]
    assert s.epic == "22"
    assert s.feature == "22.1"
    assert s.title == "Second story"
    assert s.points == 5
    # First word only — the trailing em-dash aside is discarded.
    assert s.risk == "High"

    # Feature B derives a distinct feature from the same epic.
    assert by_id["22.2-001"].feature == "22.2"
    assert by_id["22.2-001"].epic == "22"


def test_parse_no_story_dir_returns_empty(tmp_path: Path) -> None:
    assert parse_inventory_specs(tmp_path) == []


def test_parse_story_without_points_or_risk_still_projects(tmp_path: Path) -> None:
    """A story that omits Points/Risk projects with ``None`` — not dropped."""
    _write_epic(
        tmp_path,
        "epic-22-sparse.md",
        "# Epic 22: Sparse\n\n"
        "##### Story 22.9-001: Bare story\n"
        "**User Story**: As FX, I want a bare thing.\n",
    )

    specs = parse_inventory_specs(tmp_path)
    by_id = {s.story_id: s for s in specs}

    assert "22.9-001" in by_id
    s = by_id["22.9-001"]
    assert s.points is None
    assert s.risk is None
    assert s.title == "Bare story"


def test_parse_accepts_bare_points_label(tmp_path: Path) -> None:
    """``**Points**: N`` (no ``Story`` prefix) is accepted, same as discovery.py."""
    _write_epic(
        tmp_path,
        "epic-22-alt.md",
        "# Epic 22: Alt\n\n"
        "##### Story 22.8-001: Alt-label story\n"
        "**Points**: 7\n\n"
        "**Risk Level**: Low\n",
    )

    by_id = {s.story_id: s for s in parse_inventory_specs(tmp_path)}
    assert by_id["22.8-001"].points == 7


def test_parse_first_points_and_risk_line_wins(tmp_path: Path) -> None:
    """Duplicate Points/Risk lines in one block: the first wins, later ones ignored."""
    _write_epic(
        tmp_path,
        "epic-22-dup.md",
        "# Epic 22: Dup\n\n"
        "##### Story 22.7-001: Dup story\n"
        "**Story Points**: 3\n"
        "**Risk Level**: Low\n"
        "**Story Points**: 99\n"
        "**Risk Level**: Critical\n",
    )

    by_id = {s.story_id: s for s in parse_inventory_specs(tmp_path)}
    s = by_id["22.7-001"]
    assert s.points == 3
    assert s.risk == "Low"


def test_parse_merges_multiple_epic_files_in_sorted_order(tmp_path: Path) -> None:
    """Stories from every ``epic-*.md`` are merged; files read in sorted order."""
    _write_epic(
        tmp_path,
        "epic-23-second.md",
        "# Epic 23\n\n##### Story 23.1-001: From epic 23\n**Story Points**: 1\n",
    )
    _write_epic(tmp_path, "epic-22-first.md", _SAMPLE_EPIC)

    specs = parse_inventory_specs(tmp_path)
    ids = [s.story_id for s in specs]

    assert set(ids) == {"22.1-001", "22.1-002", "22.2-001", "23.1-001"}
    # epic-22-first.md sorts before epic-23-second.md, so its stories come first.
    assert ids.index("22.1-001") < ids.index("23.1-001")


# --- projection (create) -----------------------------------------------------


def test_project_creates_a_row_per_story(tmp_path: Path) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)
    ledger = _ledger(tmp_path)

    result = project_specs(ledger, tmp_path)

    assert sorted(result.added) == ["22.1-001", "22.1-002", "22.2-001"]
    assert result.updated == []
    assert result.removed == []
    assert _count(ledger.db_path) == 3

    row = _row(ledger.db_path, "22.1-002")
    assert row is not None
    assert row["epic"] == "22"
    assert row["feature"] == "22.1"
    assert row["title"] == "Second story"
    assert row["points"] == 5
    assert row["risk"] == "High"
    # Cache columns start empty — sync/build own them, not the projector.
    assert row["status"] is None
    assert row["owner"] is None
    assert row["host"] is None
    assert row["issue_ref"] is None


# --- projection (idempotent re-run) ------------------------------------------


def test_rerun_updates_in_place_no_duplicates(tmp_path: Path) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)
    ledger = _ledger(tmp_path)

    project_specs(ledger, tmp_path)
    result = project_specs(ledger, tmp_path)

    # Second pass: every story already present, so updated (not added), no dupes.
    assert result.added == []
    assert sorted(result.updated) == ["22.1-001", "22.1-002", "22.2-001"]
    assert result.removed == []
    assert _count(ledger.db_path) == 3


def test_rerun_reflects_edited_spec(tmp_path: Path) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)
    ledger = _ledger(tmp_path)
    project_specs(ledger, tmp_path)

    edited = _SAMPLE_EPIC.replace("Second story", "Second story (renamed)").replace(
        "**Story Points**: 5", "**Story Points**: 8"
    )
    _write_epic(tmp_path, "epic-22-sample.md", edited)
    project_specs(ledger, tmp_path)

    row = _row(ledger.db_path, "22.1-002")
    assert row["title"] == "Second story (renamed)"
    assert row["points"] == 8


# --- added / removed handling ------------------------------------------------


def test_added_story_is_added_removed_story_is_flagged_not_dropped(
    tmp_path: Path,
) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)
    ledger = _ledger(tmp_path)
    project_specs(ledger, tmp_path)

    # Drop 22.2-001, add 22.1-003.
    new_epic = _SAMPLE_EPIC.replace(
        """##### Story 22.2-001: Third story
**User Story**: As a contributor, I want a third thing.
**Priority**: Should Have
**Story Points**: 2

**Risk Level**: Low
**Dependencies**: None
""",
        """##### Story 22.1-003: New story
**User Story**: As FX, I want a fourth thing.
**Priority**: Should Have
**Story Points**: 1

**Risk Level**: Low
**Dependencies**: None
""",
    )
    _write_epic(tmp_path, "epic-22-sample.md", new_epic)

    result = project_specs(ledger, tmp_path)

    assert result.added == ["22.1-003"]
    assert "22.2-001" in result.removed
    # Removed stories are flagged, NOT silently dropped — the row survives.
    assert _row(ledger.db_path, "22.2-001") is not None
    assert _count(ledger.db_path) == 4


# --- cache preservation ------------------------------------------------------


def test_project_preserves_host_issue_ref_owner_status(tmp_path: Path) -> None:
    _write_epic(tmp_path, "epic-22-sample.md", _SAMPLE_EPIC)
    ledger = _ledger(tmp_path)
    project_specs(ledger, tmp_path)

    # Simulate the mirror/sync having linked an issue + cached the host fields.
    conn = sqlite3.connect(ledger.db_path)
    conn.execute(
        "UPDATE story_inventory SET host='github', issue_ref='42', owner='alice', "
        "status='in-review', harness='build:claude' WHERE story_id='22.1-002'"
    )
    conn.commit()
    conn.close()

    # Re-project after an MD edit to the same story.
    edited = _SAMPLE_EPIC.replace("Second story", "Second story (renamed)")
    _write_epic(tmp_path, "epic-22-sample.md", edited)
    project_specs(ledger, tmp_path)

    row = _row(ledger.db_path, "22.1-002")
    # Spec columns refreshed from the MD...
    assert row["title"] == "Second story (renamed)"
    # ...while the sync/build-owned cache columns are untouched.
    assert row["host"] == "github"
    assert row["issue_ref"] == "42"
    assert row["owner"] == "alice"
    assert row["status"] == "in-review"
    assert row["harness"] == "build:claude"


# --- integration against the real backlog ------------------------------------


def test_projects_the_real_repo_backlog(tmp_path: Path) -> None:
    """Smoke test over the actual docs/stories epics — every story id projects."""
    repo_root = Path(__file__).resolve().parents[2]
    if not (repo_root / "docs" / "stories").is_dir():
        return  # running from an installed wheel without the source tree

    ledger = _ledger(tmp_path)
    result = project_specs(ledger, repo_root)

    # The real backlog is well over a hundred stories across all epics.
    assert _count(ledger.db_path) > 100
    assert len(result.added) == _count(ledger.db_path)

    # This very story is present, parsed with its epic/feature/risk.
    row = _row(ledger.db_path, "22.1-002")
    assert row is not None
    assert row["epic"] == "22"
    assert row["feature"] == "22.1"
    assert row["points"] == 5
    assert row["risk"] == "Medium"
