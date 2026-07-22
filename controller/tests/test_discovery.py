# ABOUTME: Tests for story discovery from epic markdown (Story 7.3-001).
# ABOUTME: Parses `##### Story X.Y-NNN:` headers into the build queue.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.cohort import compute_cohorts
from sdlc.discovery import canonical_scope, discover_queue, parse_epic_file

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


# --- Story 27.3-002: section capture for prompt injection --------------------


def test_parse_epic_file_captures_story_section_verbatim(tmp_path) -> None:
    """Each story carries its own markdown section, header line included."""
    epic = _write_epic(tmp_path)
    stories = parse_epic_file(epic)
    assert stories[0].section == (
        "##### Story 99.1-001: First story\n"
        "**Priority**: P1\n"
        "**Points**: 2\n"
        "**Dependencies**: None.\n"
        "\n"
        "Body text."
    )
    assert stories[1].section.startswith("##### Story 99.1-002: Second story")
    assert stories[1].section.endswith("More body.")


def test_story_section_stops_at_non_story_heading(tmp_path) -> None:
    """A feature/epic-level heading ends the section — it never leaks in."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    epic = stories_dir / "epic-98-sample.md"
    epic.write_text(
        "# Epic 98: Sample\n\n"
        "##### Story 98.1-001: Only story\n"
        "**Priority**: P1\n"
        "**Risk Level**: Low\n\n"
        "## Verification & Exit Measurements\n\n"
        "Post-epic prose that belongs to the epic, not the story.\n",
        encoding="utf-8",
    )
    (story,) = parse_epic_file(epic)
    assert story.section.endswith("**Risk Level**: Low")
    assert "Verification" not in story.section
    assert "Post-epic prose" not in story.section


def test_story_metadata_still_parsed_after_section_ends(tmp_path) -> None:
    """Metadata after a section-ending heading still feeds the Story record.

    Section capture stops at the first non-story heading, but done-detection
    (DoD boxes) keeps scanning until the next story header — the two loops are
    deliberately independent (Story 27.3-002).
    """
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    epic = stories_dir / "epic-97-sample.md"
    epic.write_text(
        "# Epic 97: Sample\n\n"
        "##### Story 97.1-001: Only story\n"
        "**Priority**: P1\n\n"
        "###### Definition of Done\n"
        "- [x] Shipped\n"
        "- [x] Verified\n",
        encoding="utf-8",
    )
    (story,) = parse_epic_file(epic)
    assert story.done is True
    assert story.section.endswith("**Priority**: P1")
    assert "Definition of Done" not in story.section


def test_story_section_survives_headings_inside_code_fences(tmp_path) -> None:
    """A `# heading`-looking line inside a fenced code block never ends capture.

    Real regression: epic-05 story 5.3-001 embeds a ```markdown example whose
    first line is `# Changelog` — the pre-fix parser stopped there and silently
    injected a spec truncated mid-code-block, exactly what the size-cap fallback
    exists to prevent (Story 27.3-002 AC2: no truncated specs ever injected).
    """
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    epic = stories_dir / "epic-96-sample.md"
    epic.write_text(
        "# Epic 96: Sample\n\n"
        "##### Story 96.1-001: Only story\n"
        "**Priority**: P1\n\n"
        "Format example:\n\n"
        "```markdown\n"
        "# Changelog\n"
        "## [Unreleased]\n"
        "```\n\n"
        "```bash\n"
        "# a shell comment, not a heading\n"
        "echo ok\n"
        "```\n\n"
        "**Risk Level**: Low\n\n"
        "## Verification\n\n"
        "Epic-level prose.\n",
        encoding="utf-8",
    )
    (story,) = parse_epic_file(epic)
    assert "# Changelog" in story.section
    assert "# a shell comment, not a heading" in story.section
    assert story.section.endswith("**Risk Level**: Low")
    assert "Epic-level prose" not in story.section


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


# --- 19.1-001: multiple explicit epic/story scopes --------------------------


def _write_two_epics(tmp_path):
    """epic-99 (two stories) + epic-34 (three stories) in docs/stories."""
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    (stories / "epic-34-user-management.md").write_text(_EPIC_34_LIKE, encoding="utf-8")
    return stories


def test_discover_queue_unions_multiple_epics(tmp_path, monkeypatch) -> None:
    """AC1: `epic-99,epic-34` is the union of both epics' stories."""
    _write_two_epics(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("epic-99,epic-34")
    assert {s.id for s in queue} == {
        "99.1-001", "99.1-002", "34.1-001", "34.2-001", "34.5-003",
    }


def test_discover_queue_space_separated_scopes(tmp_path, monkeypatch) -> None:
    """AC2: space-separated tokens resolve the same as comma-separated."""
    _write_two_epics(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert {s.id for s in discover_queue("epic-99 epic-34")} == {
        s.id for s in discover_queue("epic-99,epic-34")
    }


def test_discover_queue_dedups_overlapping_scopes(tmp_path, monkeypatch) -> None:
    """AC1: an epic + a story already in it dedups by story id, order preserved."""
    _write_two_epics(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("epic-99,99.1-001")
    ids = [s.id for s in queue]
    assert ids == ["99.1-001", "99.1-002"]  # no duplicate, epic order preserved


def test_discover_queue_all_mixed_with_epic_yields_all(tmp_path, monkeypatch) -> None:
    """AC3: `all` mixed with an explicit epic resolves to every epic."""
    _write_two_epics(tmp_path)
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("all,epic-99")
    assert {s.id for s in queue} == {
        "99.1-001", "99.1-002", "34.1-001", "34.2-001", "34.5-003",
    }


def test_canonical_scope_sorts_dedups_and_lowercases() -> None:
    """A composite label is lowercased, deduped, sorted, comma-joined."""
    assert canonical_scope("Epic-18 epic-15") == "epic-15,epic-18"
    assert canonical_scope("epic-18,epic-15,epic-18") == "epic-15,epic-18"
    assert canonical_scope(["epic-18", "epic-15"]) == "epic-15,epic-18"


def test_canonical_scope_order_independent() -> None:
    """AC5: any token order maps to the same canonical label."""
    assert canonical_scope("epic-18 epic-15") == canonical_scope("epic-15 epic-18")


def test_canonical_scope_all_collapses() -> None:
    """AC3: `all` (alone or mixed) collapses to `all`; empty defaults to `all`."""
    assert canonical_scope("all") == "all"
    assert canonical_scope("epic-99 all epic-34") == "all"
    assert canonical_scope("") == "all"
    assert canonical_scope([]) == "all"


def test_canonical_scope_single_scope_unchanged() -> None:
    """A single scope round-trips to its lowercased self (backward compatible)."""
    assert canonical_scope("epic-99") == "epic-99"
    assert canonical_scope("34.5-003") == "34.5-003"


# --- Story 28.2-001: predictor features (points demoted to metadata) ---------

_FEATURE_EPIC = """# Epic 98: Predictor features

##### Story 98.1-001: Root story
**Priority**: P1
**Story Points**: 3
**Acceptance Criteria**:
- **Given** a thing **When** it happens **Then** it works.
- **Given** another thing **When** it happens **Then** it also works,
  even when the criterion wraps onto a continuation line.

**Technical Notes**: Touch `controller/src/sdlc/discovery.py` and
`controller/src/sdlc/cohort.py`, then re-read `controller/src/sdlc/discovery.py`.
**Dependencies**: None

##### Story 98.1-002: Middle story
**Priority**: P2
**Story Points**: 5
**Acceptance Criteria**:
- **Given** x **When** y **Then** z.

**Dependencies**: 98.1-001

##### Story 98.1-003: Leaf story
**Priority**: P2
**Story Points**: 2
**Dependencies**: 98.1-002

##### Story 98.1-004: Story with no dependencies line
**Priority**: P3
**Story Points**: 1

Body only.
"""

_CYCLE_EPIC = """# Epic 97: Cycle

##### Story 97.1-001: A
**Priority**: P1
**Dependencies**: 97.1-002

##### Story 97.1-002: B
**Priority**: P1
**Dependencies**: 97.1-001
"""


def _write_feature_epic(tmp_path, text: str = _FEATURE_EPIC, name: str = "epic-98-features.md"):
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    epic = stories / name
    epic.write_text(text, encoding="utf-8")
    return epic


def _by_id(stories) -> dict:
    return {s.id: s for s in stories}


def test_features_acceptance_criteria_count(tmp_path) -> None:
    """AC1: the story record carries the acceptance-criteria count."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    assert stories["98.1-001"].ac_count == 2
    assert stories["98.1-002"].ac_count == 1


def test_features_acceptance_criteria_unknown_when_absent(tmp_path) -> None:
    """AC4: no Acceptance Criteria block → unknown (None), never a real 0."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    assert stories["98.1-003"].ac_count is None


def test_features_dependency_depth_from_graph(tmp_path) -> None:
    """AC1: dependency depth is the longest chain through the epic's graph."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    assert stories["98.1-001"].dep_depth == 0
    assert stories["98.1-002"].dep_depth == 1
    assert stories["98.1-003"].dep_depth == 2


def test_features_dependency_depth_unknown_without_dependencies_line(tmp_path) -> None:
    """AC4: a story that states no Dependencies line has unknown depth, not 0."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    assert stories["98.1-004"].dep_depth is None


def test_features_dependency_depth_unknown_for_cycle_members(tmp_path) -> None:
    """A dependency cycle yields unknown depth and never crashes discovery."""
    epic = _write_feature_epic(tmp_path, _CYCLE_EPIC, "epic-97-cycle.md")
    stories = _by_id(parse_epic_file(epic))
    assert stories["97.1-001"].dep_depth is None
    assert stories["97.1-002"].dep_depth is None


def test_features_scope_proxy_counts_distinct_paths(tmp_path) -> None:
    """AC1: the scope proxy counts the distinct files/areas the epic states."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    # discovery.py is named twice; the proxy counts distinct paths.
    assert stories["98.1-001"].scope_proxy == 2


def test_features_scope_proxy_unknown_when_epic_states_none(tmp_path) -> None:
    """AC4: a story that names no files/areas has an unknown scope proxy."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    assert stories["98.1-002"].scope_proxy is None


def test_features_do_not_disturb_existing_fields(tmp_path) -> None:
    """AC2/AC3: points and every pre-existing field survive unchanged."""
    stories = _by_id(parse_epic_file(_write_feature_epic(tmp_path)))
    story = stories["98.1-002"]
    assert story.points == 5  # descriptive scope label, still parsed
    assert story.priority == "P2"
    assert story.dependencies == ["98.1-001"]
    assert story.title == "Middle story"
    assert story.epic_id == "98"


def test_features_default_to_unknown_on_synthesized_stories() -> None:
    """AC3: the new fields are additive — a Story built the old way still works."""
    from sdlc.cohort import Story

    story = Story(
        id="1.1-001", title="t", epic_id="01", epic_name="e", epic_file="f",
        priority="P1", points=3, agent_type="general-purpose",
    )
    assert story.ac_count is None
    assert story.dep_depth is None
    assert story.scope_proxy is None


# --- Story 28.2-001: feature-extraction edges --------------------------------
#
# The block above pins the happy paths off one epic; these pin the boundaries
# each extractor's docstring claims but the happy-path epic never exercises —
# where the AC block stops, which inline-code spans count as a path, and how
# depth resolves across a graph that is not a single straight chain.

_AC_SHAPES_EPIC = """# Epic 96: AC block shapes

##### Story 96.1-001: Closed by Definition of Done
**Priority**: P1
**Acceptance Criteria**:
- **Given** a **When** b **Then** c.
* **Given** d **When** e **Then** f.
  - a sub-bullet elaborating the criterion above
  continuation prose for the criterion

**Definition of Done**:
- [ ] a checklist item that is not an acceptance criterion
- [ ] another checklist item
**Dependencies**: None

##### Story 96.1-002: Closed by the next heading
**Priority**: P2
**Acceptance Criteria**:
- **Given** g **When** h **Then** i.

##### Story 96.1-003: Empty acceptance-criteria block
**Priority**: P3
**Acceptance Criteria**:

**Technical Notes**: nothing stated.
**Dependencies**: None
"""


def test_features_ac_count_stops_before_definition_of_done(tmp_path) -> None:
    """The AC block ends at the next `**Label**:` — DoD checkboxes are not criteria.

    Both blocks are made of column-0 bullets, so a counter that did not stop at
    the `**Definition of Done**:` label would silently report 4 here and inflate
    the feature for every real story in `docs/stories/`.
    """
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _AC_SHAPES_EPIC, "epic-96-ac.md")
    ))
    # Two criteria: one `-` bullet and one `*` bullet. The indented sub-bullet
    # and the continuation line belong to the second criterion, not to new ones,
    # and neither DoD checkbox is counted.
    assert stories["96.1-001"].ac_count == 2


def test_features_ac_count_stops_at_next_story_heading(tmp_path) -> None:
    """A story whose AC block runs to the end of its section stops at the heading."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _AC_SHAPES_EPIC, "epic-96-ac.md")
    ))
    assert stories["96.1-002"].ac_count == 1


def test_features_ac_count_unknown_for_empty_block(tmp_path) -> None:
    """AC4: an AC label with no criteria under it is unknown, not a real 0."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _AC_SHAPES_EPIC, "epic-96-ac.md")
    ))
    assert stories["96.1-003"].ac_count is None


_SCOPE_SPANS_EPIC = """# Epic 95: Scope proxy spans

##### Story 95.1-001: Prose spans are not paths
**Priority**: P1
**Technical Notes**: `points` stays descriptive, so `story.points` is no longer
read by the router; see Story `12.3-001`. Run `sdlc build --scope epic-95` and
`uv run pytest` afterwards.
**Dependencies**: None

##### Story 95.1-002: Bare filenames and directories count
**Priority**: P2
**Technical Notes**: Update `README.md`, `flake.nix` and `controller/src/sdlc/`,
then re-read `README.md`.
**Dependencies**: None
"""


def test_features_scope_proxy_ignores_prose_code_spans(tmp_path) -> None:
    """Inline code that is prose — not a path — must not inflate the scope proxy.

    `points`, `story.points` and `12.3-001` all sit in inline code but name no
    file; multi-word spans like `sdlc build --scope epic-95` are commands. A
    proxy that counted them would report scope for a story touching nothing.
    """
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _SCOPE_SPANS_EPIC, "epic-95-scope.md")
    ))
    assert stories["95.1-001"].scope_proxy is None


def test_features_scope_proxy_counts_bare_filenames_and_directories(tmp_path) -> None:
    """A known source/doc extension or a `/` marks a path; repeats still collapse."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _SCOPE_SPANS_EPIC, "epic-95-scope.md")
    ))
    # README.md (named twice → once), flake.nix, controller/src/sdlc/
    assert stories["95.1-002"].scope_proxy == 3


_DEPTH_GRAPH_EPIC = """# Epic 94: Depth graph shapes

##### Story 94.1-001: Root
**Priority**: P1
**Dependencies**: None

##### Story 94.1-002: Short arm
**Priority**: P1
**Dependencies**: 94.1-001

##### Story 94.1-003: Diamond join
**Priority**: P1
**Dependencies**: 94.1-001, 94.1-002

##### Story 94.1-004: Depends on another epic
**Priority**: P2
**Dependencies**: 12.3-001

##### Story 94.1-005: No dependencies line stated
**Priority**: P2

##### Story 94.1-006: Depends on an unknown-depth story
**Priority**: P2
**Dependencies**: 94.1-005
"""


def test_features_dep_depth_takes_the_longest_chain(tmp_path) -> None:
    """Depth is the *longest* chain, not the shortest — a diamond resolves to 2."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _DEPTH_GRAPH_EPIC, "epic-94-depth.md")
    ))
    assert stories["94.1-002"].dep_depth == 1
    # Reachable via the depth-0 root (→1) and via the depth-1 arm (→2); max wins.
    assert stories["94.1-003"].dep_depth == 2


def test_features_dep_depth_counts_cross_epic_dependency_as_one_level(tmp_path) -> None:
    """An edge to a story outside this epic is real, so it is depth 1, not 0."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _DEPTH_GRAPH_EPIC, "epic-94-depth.md")
    ))
    assert stories["94.1-004"].dep_depth == 1


def test_features_dep_depth_counts_unknown_predecessor_as_one_level(tmp_path) -> None:
    """Depending on an unknown-depth story still yields a known depth of 1.

    The predecessor's own chain is unknown, but *this* story's edge is stated —
    so the feature is a real 1 rather than propagating unknown down the graph.
    """
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _DEPTH_GRAPH_EPIC, "epic-94-depth.md")
    ))
    assert stories["94.1-005"].dep_depth is None  # stated no Dependencies line
    assert stories["94.1-006"].dep_depth == 1


_PARTIAL_CYCLE_EPIC = """# Epic 93: One cycle, one clean chain

##### Story 93.1-001: Cycle member A
**Priority**: P1
**Dependencies**: 93.1-002

##### Story 93.1-002: Cycle member B
**Priority**: P1
**Dependencies**: 93.1-001

##### Story 93.1-003: Clean root
**Priority**: P1
**Dependencies**: None

##### Story 93.1-004: Clean leaf
**Priority**: P1
**Dependencies**: 93.1-003
"""


def test_features_dep_depth_resolves_stories_off_a_cycle(tmp_path) -> None:
    """A cycle strands only its own members; the rest of the epic still resolves."""
    stories = _by_id(parse_epic_file(
        _write_feature_epic(tmp_path, _PARTIAL_CYCLE_EPIC, "epic-93-partial.md")
    ))
    assert stories["93.1-001"].dep_depth is None
    assert stories["93.1-002"].dep_depth is None
    assert stories["93.1-003"].dep_depth == 0
    assert stories["93.1-004"].dep_depth == 1


def test_discover_queue_carries_predictor_features(tmp_path, monkeypatch) -> None:
    """AC1 end-to-end: the features survive the public discovery entry point.

    `parse_epic_file` is the extractor, but every caller reaches it through
    `discover_queue` — including the single-story scope the controller uses to
    rebuild one story, which re-parses the whole epic and must still produce the
    whole-graph `dep_depth`.
    """
    _write_feature_epic(tmp_path)
    monkeypatch.chdir(tmp_path)

    queue = _by_id(discover_queue("epic-98"))
    assert (queue["98.1-001"].ac_count, queue["98.1-001"].scope_proxy) == (2, 2)
    assert queue["98.1-003"].dep_depth == 2

    # Single-story scope: the depth still reflects the full epic graph, not the
    # one-story slice the caller asked for.
    single = discover_queue("98.1-003")
    assert [s.id for s in single] == ["98.1-003"]
    assert single[0].dep_depth == 2
    assert single[0].points == 2  # descriptive label, unchanged by scoping
