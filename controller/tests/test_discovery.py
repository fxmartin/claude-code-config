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
