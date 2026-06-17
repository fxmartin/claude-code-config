# ABOUTME: Tests for story discovery from epic markdown (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers into the build queue.

from __future__ import annotations

from sdlc.discovery import discover_queue, parse_epic_file

_SAMPLE_EPIC = """# Epic 99: Sample

##### Story 99.1-001: First story
**Priority**: P1
**Points**: 2
**Dependencies**: None.

Body text.

##### Story 99.1-002: Second story
**Priority**: P2
**Points**: 3
**Dependencies**: Story 99.1-001.

More body.
"""


def _write_epic(tmp_path) -> "Path":  # type: ignore[name-defined]
    from pathlib import Path

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    epic = stories / "epic-99-sample.md"
    epic.write_text(_SAMPLE_EPIC, encoding="utf-8")
    return epic


def test_parse_epic_file_extracts_stories(tmp_path) -> None:
    epic = _write_epic(tmp_path)
    stories = parse_epic_file(epic)
    assert [s.id for s in stories] == ["99.1-001", "99.1-002"]
    assert stories[0].title == "First story"
    assert stories[0].priority == "P1"
    assert stories[0].points == 2
    assert stories[0].epic_id == "99"


def test_parse_epic_file_extracts_dependencies(tmp_path) -> None:
    epic = _write_epic(tmp_path)
    stories = parse_epic_file(epic)
    assert stories[0].dependencies == []
    assert "99.1-001" in stories[1].dependencies


def test_discover_queue_scopes_by_epic(tmp_path, monkeypatch) -> None:
    _write_epic(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("epic-99")
    assert {s.id for s in queue} == {"99.1-001", "99.1-002"}


def test_discover_queue_all_reads_every_epic(tmp_path, monkeypatch) -> None:
    _write_epic(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("all")
    assert len(queue) == 2


def test_discover_queue_unknown_epic_returns_empty(tmp_path, monkeypatch) -> None:
    _write_epic(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert discover_queue("epic-77") == []


# --- R5: Story Points, and R4: done-detection -------------------------------

_EPIC_34_LIKE = """# Epic 34: User Management

##### Story 34.1-001: Shipped via Status line
**Priority**: Must Have
**Story Points**: 8

**Definition of Done**:
- [ ] not actually checked

**Status**: Done — `feature/34.1-001`.

##### Story 34.2-001: Shipped via all-checked DoD
**Priority**: Must Have
**Story Points**: 5

**Definition of Done**:
- [x] Code implemented and peer reviewed
- [x] Tests

##### Story 34.5-003: Not yet built
**Priority**: Should Have
**Story Points**: 3

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [x] partial only

**Dependencies**: 34.1-001, 34.5-002
**Status**: Not Started — backup path.
"""


def _write_epic34(tmp_path):
    from pathlib import Path

    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    epic = stories / "epic-34-user-management.md"
    epic.write_text(_EPIC_34_LIKE, encoding="utf-8")
    return epic


def test_story_points_alias_is_parsed(tmp_path) -> None:
    """R5: `**Story Points**: N` is read (not just `**Points**:`)."""
    by_id = {s.id: s for s in parse_epic_file(_write_epic34(tmp_path))}
    assert by_id["34.5-003"].points == 3
    assert by_id["34.1-001"].points == 8


def test_done_detection(tmp_path) -> None:
    """R4: Status 'Done' OR all-checked DoD → done; otherwise not done."""
    by_id = {s.id: s for s in parse_epic_file(_write_epic34(tmp_path))}
    assert by_id["34.1-001"].done is True   # Status: Done wins despite an unchecked box
    assert by_id["34.2-001"].done is True   # all DoD boxes checked
    assert by_id["34.5-003"].done is False  # a box is unchecked, Status: Not Started


# --- R2: single-story scope -------------------------------------------------


def test_discover_queue_single_story_scope(tmp_path, monkeypatch) -> None:
    """R2: a bare `X.Y-NNN` scope resolves its epic and returns only that story."""
    _write_epic34(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("34.5-003")
    assert [s.id for s in queue] == ["34.5-003"]


def test_discover_queue_single_story_not_found(tmp_path, monkeypatch) -> None:
    """A story id with no matching story returns an empty queue."""
    _write_epic34(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert discover_queue("34.9-999") == []


def test_discover_queue_single_story_zero_padded_epic(tmp_path, monkeypatch) -> None:
    """Story scope resolves zero-padded epic filenames (epic-07 ↔ major 7)."""
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-07-controller.md").write_text(
        "##### Story 7.3-001: Port it\n**Story Points**: 2\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert [s.id for s in discover_queue("7.3-001")] == ["7.3-001"]
