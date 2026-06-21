# ABOUTME: Tests for story discovery from epic markdown (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers into the build queue.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.cohort import compute_cohorts
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
    assert by_id["34.1-001"].done is True  # Status: Done wins despite an unchecked box
    assert by_id["34.2-001"].done is True  # all DoD boxes checked
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


# --- 12.5-001: parse only intended dependency edges -------------------------


def _deps_for(content: str, self_id: str = "99.9-999") -> list[str]:
    """Parse a single `**Dependencies**:` value via parse_epic_file."""
    epic = (
        "# Epic 99: Deps\n\n"
        f"##### Story {self_id}: Probe\n"
        "**Priority**: P1\n"
        "**Story Points**: 1\n"
        f"**Dependencies**: {content}\n"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "epic-99-deps.md"
        path.write_text(epic, encoding="utf-8")
        stories = parse_epic_file(path)
    assert [s.id for s in stories] == [self_id]
    return stories[0].dependencies


def test_leading_id_then_parenthetical_prose_ignored() -> None:
    """AC1: only the leading edge id is read; ids in prose are ignored."""
    # The verbose 12.3-003 line that motivated this story.
    assert _deps_for("12.3-001 (reconcile flips it once 12.3-004 lands)") == [
        "12.3-001"
    ]


def test_leading_none_with_prose_ids_yields_no_deps() -> None:
    """AC2: a leading `None` followed by prose ids resolves to zero edges."""
    assert _deps_for("None (shares build.py with 12.3-004 and 12.4-001)") == []
    # The real 12.4-001 line: `None to ship; ...` before any id.
    assert (
        _deps_for("None to ship; pairs with 12.3-001 (reconcile preserves work)") == []
    )


def test_multiple_leading_edges_with_annotations_all_kept() -> None:
    """Real 11.2-005 line: two real edges, each annotated, then trailing prose."""
    assert _deps_for(
        "11.2-001 (repo path from the registry), 11.2-002 (run selection). The"
    ) == ["11.2-001", "11.2-002"]


def test_terse_connective_lists_unchanged() -> None:
    """Terse `Stories A and B.` and comma lists keep every leading edge."""
    assert _deps_for("Stories 3.1-001 and 3.1-002.") == ["3.1-001", "3.1-002"]
    assert _deps_for("11.1-002, 11.2-003") == ["11.1-002", "11.2-003"]
    assert _deps_for("Story 99.1-001.", self_id="99.1-002") == ["99.1-001"]


def test_ids_only_inside_parens_are_not_edges() -> None:
    """Real epic-10 line: ids appear only inside a parenthetical → no edges."""
    assert (
        _deps_for("Epic-07 (Stories 7.1-001, 7.3-001) and Epic-04 (ledger schema).")
        == []
    )


def test_independent_of_prose_ids_are_not_edges() -> None:
    """`Independent of Stories 9.1-001 and 9.1-002.` are not dependencies."""
    assert (
        _deps_for("Epic-02 (CI workflow). Independent of Stories 9.1-001 and 9.1-002.")
        == []
    )


def test_none_marker_variants_yield_no_deps() -> None:
    assert _deps_for("None") == []
    assert _deps_for("none") == []
    assert _deps_for("none (independent of other epics).") == []
    assert _deps_for("N/A") == []
    assert _deps_for("TBD — to be decided once 14.1-001 lands") == []


def _epic_files() -> list[Path]:
    here = Path(__file__).resolve()
    # controller/tests/test_discovery.py -> repo root is two parents up.
    stories_dir = here.parents[2] / "docs" / "stories"
    return sorted(stories_dir.glob("epic-*.md"))


def test_all_epics_schedule_without_phantom_cycle() -> None:
    """AC4: every shipped epic file parses and schedules with no phantom cycle."""
    epics = _epic_files()
    assert epics, "no epic story files found"
    for epic in epics:
        stories = parse_epic_file(epic)
        # compute_cohorts must not raise a phantom cycle from prose-mentioned ids.
        compute_cohorts(stories)


def test_no_resolved_edge_appears_only_in_prose() -> None:
    """AC3 guard: across all epics, every resolved edge id appears in the leading
    head of its Dependencies line — never only in parenthetical/sentence prose.
    """
    import re

    from sdlc.discovery import _dependency_head

    header = re.compile(r"^#{2,6}\s*Story\s+([0-9]+\.[0-9]+-[0-9]+):")
    dep_line = re.compile(r"^\*\*Dependencies\*\*:\s*(.+?)\s*$")
    dep_id = re.compile(r"[0-9]+\.[0-9]+-[0-9]+")

    # Map each story id to the set of ids in the *leading head* of its
    # Dependencies line; a resolved edge outside that set would be prose-only.
    checked = 0
    for epic in _epic_files():
        current: str | None = None
        heads: dict[str, set[str]] = {}
        for line in epic.read_text(encoding="utf-8").splitlines():
            if h := header.match(line):
                current = h.group(1)
                continue
            if current and (m := dep_line.match(line)):
                heads[current] = set(dep_id.findall(_dependency_head(m.group(1))))
                current = None
        for story in parse_epic_file(epic):
            allowed = heads.get(story.id, set())
            prose_only = set(story.dependencies) - allowed
            assert not prose_only, f"{epic.name} {story.id}: prose-only {prose_only}"
            checked += 1
    assert checked > 0


def test_genuine_cycle_still_fails_fast() -> None:
    """AC5: a real intended cycle still raises the story-named cohort error."""
    epic = (
        "# Epic 99: Cycle\n\n"
        "##### Story 99.1-001: A\n**Story Points**: 1\n**Dependencies**: 99.1-002\n\n"
        "##### Story 99.1-002: B\n**Story Points**: 1\n**Dependencies**: 99.1-001\n"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "epic-99-cycle.md"
        path.write_text(epic, encoding="utf-8")
        stories = parse_epic_file(path)
    with pytest.raises(ValueError, match="99.1-001"):
        compute_cohorts(stories)


def test_parenthetical_only_dependencies_yield_no_edges() -> None:
    """An empty leading head (value is wholly parenthetical) resolves to zero edges."""
    assert _deps_for("(see 12.3-001 for the rationale)") == []
    # A leading prose delimiter also empties the head.
    assert _deps_for("; deferred, pairs with 12.3-001") == []


def test_discover_queue_scopes_by_bare_epic_name(tmp_path, monkeypatch) -> None:
    """A bare scope substring matches the epic filename stem."""
    _write_epic(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("sample")
    assert {s.id for s in queue} == {"99.1-001", "99.1-002"}


def test_discover_queue_without_story_dir_returns_empty(tmp_path, monkeypatch) -> None:
    """No docs/stories or stories directory under root → empty queue, no error."""
    monkeypatch.chdir(tmp_path)
    assert discover_queue("all") == []
