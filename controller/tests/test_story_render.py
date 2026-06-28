# ABOUTME: Tests for the Epic-22 issue renderer + board/label taxonomy (host-aware).
# ABOUTME: Story 22.2-002 — managed-block round-trip, marker, edit-reversion, GitHub vs GitLab surface.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.issue_host import GITHUB, GITLAB, IssueHostError
from sdlc.story_render import (
    MANAGED_CLOSE,
    MANAGED_OPEN,
    STORY_LABEL,
    StoryDoc,
    extract_managed_block,
    issue_title,
    parse_story_docs,
    render_issue_body,
    replace_managed_block,
    status_surface,
    story_labels,
    story_marker,
)

# A representative story doc — the render input the host mirror builds from the MD.
_DOC = StoryDoc(
    story_id="22.2-002",
    epic="22",
    feature="22.2",
    title="Issue rendering + board/label taxonomy",
    points=3,
    risk="Medium",
    spec_md=(
        "**User Story**: As a contributor, I want each story's issue to show its spec.\n"
        "**Story Points**: 3\n\n"
        "**Acceptance Criteria**:\n"
        "- **Given** an inventory row **When** rendered **Then** the spec sits in a block.\n\n"
        "**Definition of Done**:\n"
        "- [ ] Renderer + taxonomy implemented\n\n"
        "**Risk Level**: Medium"
    ),
)

# A minimal but format-faithful epic for the parser test.
_SAMPLE_EPIC = """# Epic 22: Sample

### Feature 22.1: A feature

##### Story 22.1-001: First story
**User Story**: As FX, I want a thing.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** X **When** Y **Then** Z.

**Definition of Done**:
- [ ] Thing implemented

**Risk Level**: Medium
**Dependencies**: None

##### Story 22.2-001: Second story
**User Story**: As FX, I want another thing.
**Story Points**: 5
**Risk Level**: High — with a trailing aside
"""


# --- managed block + marker --------------------------------------------------


def test_marker_is_present_in_rendered_body() -> None:
    body = render_issue_body(_DOC)
    assert story_marker("22.2-002") in body
    assert story_marker("22.2-002") == "<!-- sdlc-story: 22.2-002 -->"


def test_body_wraps_spec_in_managed_block() -> None:
    body = render_issue_body(_DOC)
    assert MANAGED_OPEN in body
    assert MANAGED_CLOSE in body
    assert body.index(MANAGED_OPEN) < body.index(MANAGED_CLOSE)


def test_body_contains_full_spec() -> None:
    body = render_issue_body(_DOC)
    # user story, AC, DoD, points and risk all sit inside the body.
    assert "As a contributor" in body
    assert "Acceptance Criteria" in body
    assert "Definition of Done" in body
    assert "Story Points**: 3" in body
    assert "Risk Level**: Medium" in body


def test_managed_block_round_trips() -> None:
    body = render_issue_body(_DOC)
    inner = extract_managed_block(body)
    assert inner is not None
    # The marker and the spec live inside the extracted managed region.
    assert story_marker("22.2-002") in inner
    assert "As a contributor" in inner
    # Rendering is pure/stable: same input → byte-identical body.
    assert render_issue_body(_DOC) == body


def test_extract_returns_none_without_block() -> None:
    assert extract_managed_block("just some human prose, no markers") is None


# --- managed-edit reversion (MD wins) ----------------------------------------


def test_replace_reverts_hand_edited_managed_block() -> None:
    body = render_issue_body(_DOC)
    # A human appends a discussion note *outside* the managed block and corrupts
    # the spec *inside* it.
    human_note = "\n\n## Discussion\nLooks good to me — assigning myself.\n"
    tampered = body.replace("As a contributor", "TOTALLY DIFFERENT TEXT") + human_note

    reverted = replace_managed_block(tampered, _DOC)

    # MD wins: the managed region is regenerated from the doc.
    assert "TOTALLY DIFFERENT TEXT" not in reverted
    assert "As a contributor" in reverted
    # Human content outside the block is preserved untouched.
    assert "## Discussion" in reverted
    assert "assigning myself" in reverted


def test_replace_is_idempotent() -> None:
    body = render_issue_body(_DOC)
    once = replace_managed_block(body, _DOC)
    twice = replace_managed_block(once, _DOC)
    assert once == twice
    # Exactly one managed block survives — no nesting/duplication.
    assert once.count(MANAGED_OPEN) == 1
    assert once.count(MANAGED_CLOSE) == 1


def test_replace_appends_block_when_absent() -> None:
    existing = "# A human-created issue\nSome notes here.\n"
    result = replace_managed_block(existing, _DOC)
    assert "Some notes here." in result  # human content preserved
    assert MANAGED_OPEN in result
    assert story_marker("22.2-002") in result


# --- taxonomy labels ---------------------------------------------------------


def test_story_labels_full() -> None:
    labels = story_labels("22", "22.2", 3, "Medium")
    assert labels == [STORY_LABEL, "epic:22", "feature:22.2", "points:3", "risk:medium"]


def test_story_labels_omit_missing_points_and_risk() -> None:
    labels = story_labels("22", "22.2", None, None)
    assert labels == [STORY_LABEL, "epic:22", "feature:22.2"]
    assert not any(label.startswith("points:") for label in labels)
    assert not any(label.startswith("risk:") for label in labels)


# --- host-aware status surface -----------------------------------------------


def test_status_surface_github_has_status_and_points_fields() -> None:
    surface = status_surface(GITHUB, "22", "22.2", 3, "Medium")
    assert surface.host == GITHUB
    assert surface.status_field == "Status"
    assert surface.points_field == ("Points", 3)
    assert surface.milestone is None
    # The portable `points:N` label is present on both hosts.
    assert "points:3" in surface.labels


def test_status_surface_gitlab_uses_labels_and_milestone_only() -> None:
    surface = status_surface(GITLAB, "22", "22.2", 3, "Medium")
    assert surface.host == GITLAB
    # GitLab Free: no native Projects Status field, no numeric Points field.
    assert surface.status_field is None
    assert surface.points_field is None
    # Epic maps to a milestone (+ the epic:NN label) on a GitLab Issue Board.
    assert surface.milestone == "epic-22"
    assert "epic:22" in surface.labels
    assert "points:3" in surface.labels  # the only points surface on Free


def test_status_surface_github_points_field_absent_when_unpointed() -> None:
    surface = status_surface(GITHUB, "22", "22.2", None, None)
    assert surface.points_field is None


def test_status_surface_rejects_unknown_host() -> None:
    with pytest.raises(IssueHostError):
        status_surface("bitbucket", "22", "22.2", 3, "Medium")


# --- issue title -------------------------------------------------------------


def test_issue_title_prefixes_story_id() -> None:
    assert issue_title(_DOC) == "22.2-002: Issue rendering + board/label taxonomy"


# --- parser ------------------------------------------------------------------


def test_parse_story_docs_extracts_full_block(tmp_path: Path) -> None:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-22-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")

    docs = {d.story_id: d for d in parse_story_docs(tmp_path)}
    assert set(docs) == {"22.1-001", "22.2-001"}

    first = docs["22.1-001"]
    assert first.epic == "22"
    assert first.feature == "22.1"
    assert first.title == "First story"
    assert first.points == 3
    assert first.risk == "Medium"
    # The verbatim spec body is captured (header excluded, sections included).
    assert "As FX, I want a thing." in first.spec_md
    assert "Definition of Done" in first.spec_md
    assert "First story" not in first.spec_md  # the header line is not in the body

    # Risk first-word capture survives a trailing em-dash aside.
    assert docs["22.2-001"].risk == "High"
    assert docs["22.2-001"].points == 5


def test_parse_story_docs_empty_when_no_story_dir(tmp_path: Path) -> None:
    assert parse_story_docs(tmp_path) == []


def test_parse_story_docs_accepts_plain_points_form(tmp_path: Path) -> None:
    # discovery.py accepts both `**Story Points**` and the bare `**Points**`;
    # the renderer's parser must read the same form-tolerant value.
    epic = (
        "# Epic 22: Sample\n\n"
        "##### Story 22.3-001: Plain points\n"
        "**User Story**: As FX, I want a thing.\n"
        "**Points**: 8\n"
    )
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-22.md").write_text(epic, encoding="utf-8")

    doc = parse_story_docs(tmp_path)[0]
    assert doc.points == 8


def test_parse_story_docs_points_and_risk_are_first_line_wins(tmp_path: Path) -> None:
    # A second points/risk line must not override the first (matches discovery).
    epic = (
        "# Epic 22: Sample\n\n"
        "##### Story 22.4-001: First wins\n"
        "**Story Points**: 2\n"
        "**Risk Level**: Low\n"
        "**Story Points**: 99\n"
        "**Risk Level**: Critical\n"
    )
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-22.md").write_text(epic, encoding="utf-8")

    doc = parse_story_docs(tmp_path)[0]
    assert doc.points == 2
    assert doc.risk == "Low"


def test_parse_story_docs_is_sorted_across_epic_files(tmp_path: Path) -> None:
    # Files are globbed in sorted order, so the projection is deterministic.
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-02.md").write_text(
        "##### Story 2.1-001: Two\n**User Story**: x.\n", encoding="utf-8"
    )
    (stories / "epic-01.md").write_text(
        "##### Story 1.1-001: One\n**User Story**: y.\n", encoding="utf-8"
    )

    ids = [d.story_id for d in parse_story_docs(tmp_path)]
    assert ids == ["1.1-001", "2.1-001"]


# --- additional edge-case contracts ------------------------------------------


def test_render_managed_block_strips_surrounding_blank_lines() -> None:
    # spec_md is `.strip()`ed so stray leading/trailing whitespace never leaks
    # blank lines into the managed region's framing.
    doc = StoryDoc(
        story_id="22.2-002",
        epic="22",
        feature="22.2",
        title="t",
        points=None,
        risk=None,
        spec_md="\n\n  **User Story**: spec.  \n\n",
    )
    body = render_issue_body(doc)
    expected = "\n".join(
        (
            MANAGED_OPEN,
            story_marker("22.2-002"),
            "",
            "**User Story**: spec.",
            "",
            MANAGED_CLOSE,
        )
    )
    assert body == expected


def test_replace_appends_block_to_empty_body_without_leading_newlines() -> None:
    # Adopting a truly empty issue body yields just the block — no stray prefix.
    result = replace_managed_block("", _DOC)
    assert result == render_issue_body(_DOC)
    assert not result.startswith("\n")


def test_replace_only_regenerates_the_first_managed_block() -> None:
    # Non-greedy + count=1: a body that somehow holds two managed regions has
    # only the first rewritten, never collapsed or duplicated.
    body = render_issue_body(_DOC)
    doubled = body + "\n\nhuman note\n\n" + body
    result = replace_managed_block(doubled, _DOC)
    assert result.count(MANAGED_OPEN) == 2
    assert "human note" in result


def test_extract_returns_inner_for_empty_managed_region() -> None:
    # An empty managed region extracts to "" (distinct from the None of absence).
    body = f"{MANAGED_OPEN}\n{MANAGED_CLOSE}"
    assert extract_managed_block(body) == ""


def test_story_labels_lowercases_uppercase_risk() -> None:
    labels = story_labels("22", "22.2", None, "HIGH")
    assert "risk:high" in labels


def test_status_surface_is_case_insensitive_on_host() -> None:
    # Host is normalised before the supported-host check / dispatch.
    surface = status_surface("GitHub", "22", "22.2", 3, "Medium")
    assert surface.host == GITHUB
    assert surface.status_field == "Status"


def test_status_surface_rejects_none_host() -> None:
    with pytest.raises(IssueHostError):
        status_surface(None, "22", "22.2", 3, "Medium")  # type: ignore[arg-type]


def test_issue_title_handles_minimal_doc() -> None:
    doc = StoryDoc(
        story_id="1.1-001",
        epic="1",
        feature="1.1",
        title="Bare",
        points=None,
        risk=None,
        spec_md="x",
    )
    assert issue_title(doc) == "1.1-001: Bare"


def test_parsed_doc_renders_round_trip(tmp_path: Path) -> None:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-22-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    doc = next(d for d in parse_story_docs(tmp_path) if d.story_id == "22.1-001")

    body = render_issue_body(doc)
    assert story_marker("22.1-001") in body
    assert extract_managed_block(body) is not None
    assert "As FX, I want a thing." in body
