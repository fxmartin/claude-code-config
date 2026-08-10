# ABOUTME: Tests for the epic-markdown write-back helper (Issue #598).
# ABOUTME: mark_story_done sets **Status**: Done; find_epic_file resolves by story id.

from __future__ import annotations

import pytest

from sdlc.story_markdown import find_epic_file, mark_story_done


def _write(tmp_path, text: str):
    path = tmp_path / "epic-07-sample.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# mark_story_done
# ---------------------------------------------------------------------------


def test_replaces_existing_status_line(tmp_path) -> None:
    path = _write(
        tmp_path,
        "##### Story 7.1-001: Do the thing\n"
        "**Status**: Not started\n"
        "**Priority**: P1\n"
        "\n"
        "##### Story 7.1-002: Next\n"
        "**Status**: Not started\n",
    )
    assert mark_story_done(path, "7.1-001") is True
    text = path.read_text(encoding="utf-8")
    assert "##### Story 7.1-001: Do the thing\n**Status**: Done\n**Priority**: P1\n" in text
    # The other story's block is untouched.
    assert "##### Story 7.1-002: Next\n**Status**: Not started\n" in text


def test_inserts_status_line_when_absent(tmp_path) -> None:
    path = _write(
        tmp_path,
        "##### Story 7.1-001: Do the thing\n"
        "**Priority**: P1\n"
        "**Points**: 3\n",
    )
    assert mark_story_done(path, "7.1-001") is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith(
        "##### Story 7.1-001: Do the thing\n**Status**: Done\n**Priority**: P1\n"
    )


def test_inserts_status_line_when_story_is_last_line_no_trailing_newline(tmp_path) -> None:
    path = _write(tmp_path, "##### Story 7.1-001: Do the thing")
    assert mark_story_done(path, "7.1-001") is True
    assert path.read_text(encoding="utf-8") == (
        "##### Story 7.1-001: Do the thing\n**Status**: Done\n"
    )


def test_noop_when_already_done(tmp_path) -> None:
    original = (
        "##### Story 7.1-001: Do the thing\n"
        "**Status**: Done\n"
        "**Priority**: P1\n"
    )
    path = _write(tmp_path, original)
    assert mark_story_done(path, "7.1-001") is False
    assert path.read_text(encoding="utf-8") == original


def test_noop_when_already_done_with_trailing_detail(tmp_path) -> None:
    """`**Status**: Done (run abc123)` counts as Done — matches discovery's prefix rule."""
    original = "##### Story 7.1-001: Do\n**Status**: Done (run abc123)\n"
    path = _write(tmp_path, original)
    assert mark_story_done(path, "7.1-001") is False
    assert path.read_text(encoding="utf-8") == original


def test_noop_when_story_id_not_found(tmp_path) -> None:
    original = "##### Story 7.1-001: Do the thing\n**Status**: Not started\n"
    path = _write(tmp_path, original)
    assert mark_story_done(path, "7.1-999") is False
    assert path.read_text(encoding="utf-8") == original


def test_raises_on_missing_file(tmp_path) -> None:
    with pytest.raises(OSError):
        mark_story_done(tmp_path / "does-not-exist.md", "7.1-001")


def test_only_touches_the_matching_story_block(tmp_path) -> None:
    path = _write(
        tmp_path,
        "##### Story 7.1-001: First\n"
        "**Status**: Not started\n"
        "\n"
        "##### Story 7.1-002: Second\n"
        "**Status**: Not started\n"
        "\n"
        "## Verification\n"
        "Some trailing epic-level section.\n",
    )
    assert mark_story_done(path, "7.1-002") is True
    text = path.read_text(encoding="utf-8")
    assert "Story 7.1-001: First\n**Status**: Not started\n" in text
    assert "Story 7.1-002: Second\n**Status**: Done\n" in text
    assert "## Verification\nSome trailing epic-level section.\n" in text


# ---------------------------------------------------------------------------
# find_epic_file
# ---------------------------------------------------------------------------


def test_find_epic_file_resolves_by_major_number(tmp_path) -> None:
    story_dir = tmp_path / "docs" / "stories"
    story_dir.mkdir(parents=True)
    target = story_dir / "epic-07-sample.md"
    target.write_text("##### Story 7.1-001: Do\n", encoding="utf-8")
    (story_dir / "epic-08-other.md").write_text("x\n", encoding="utf-8")

    assert find_epic_file("7.1-001", tmp_path) == target


def test_find_epic_file_none_when_no_story_dir(tmp_path) -> None:
    assert find_epic_file("7.1-001", tmp_path) is None


def test_find_epic_file_none_when_no_matching_epic(tmp_path) -> None:
    story_dir = tmp_path / "docs" / "stories"
    story_dir.mkdir(parents=True)
    (story_dir / "epic-08-other.md").write_text("x\n", encoding="utf-8")

    assert find_epic_file("7.1-001", tmp_path) is None


def test_find_epic_file_none_when_major_is_not_numeric(tmp_path) -> None:
    """A malformed story id (non-numeric major) must short-circuit, not glob."""
    story_dir = tmp_path / "docs" / "stories"
    story_dir.mkdir(parents=True)
    (story_dir / "epic-07-sample.md").write_text("x\n", encoding="utf-8")

    assert find_epic_file("abc.1-001", tmp_path) is None
