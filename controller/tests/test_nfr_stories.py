# ABOUTME: Tests for buildable NFR stories (Story 36.1-001): discovery, scope, Done
# ABOUTME: markers and the issue mirror for `### Story NFR-<AREA>-NNN:` headings.

from __future__ import annotations

from pathlib import Path

import pytest
from test_story_init import FakeHost

from sdlc.build import Ledger
from sdlc.cohort import compute_cohorts
from sdlc.discovery import discover_queue, parse_epic_file
from sdlc.story_init import done_story_ids, init_issues
from sdlc.story_inventory import parse_inventory_specs
from sdlc.story_markdown import find_epic_file, render_epic_file, render_story_done
from sdlc.story_render import parse_story_docs, story_labels

# The generate-epics NFR template: `## <Area> Requirements` sections holding
# `### Story NFR-<AREA>-NNN:` headings, each with the epic-story metadata.
_NFR_MD = """\
# Non-Functional Requirements

## Overview
**Total Stories**: 3 | **Total Points**: 10

## Security Requirements
### Story NFR-SEC-001: Secrets never logged
**Priority**: Must Have
**Story Points**: 3

**Dependencies**: None
**Risk Level**: High

### Story NFR-SEC-002: Token rotation
**Priority**: Should Have
**Story Points**: 5

**Dependencies**: NFR-SEC-001, 99.1-001
**Risk Level**: Medium

## Performance Requirements
### Story NFR-PERF-001: Fast install
**Status**: Done
**Priority**: Could Have
**Story Points**: 2

**Dependencies**: None
**Risk Level**: Low
"""

_EPIC_MD = """\
# Epic 99: Sample

##### Story 99.1-001: Root story
**Priority**: P1
**Points**: 2
**Dependencies**: None

##### Story 99.1-002: Needs a security NFR
**Priority**: P2
**Points**: 3
**Dependencies**: NFR-SEC-001
"""

# An infra-named epic: `nfr` is a substring of "infra", so a naive name match
# on the `nfr` scope keyword would wrongly select it.
_INFRA_EPIC_MD = """\
# Epic 12: Infra

##### Story 12.1-001: Infra story
**Points**: 1
**Dependencies**: None
"""


def _seed(root: Path, *, nfr: str | None = _NFR_MD, subdir: str = "docs/stories") -> Path:
    story_dir = root / subdir
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "epic-99-sample.md").write_text(_EPIC_MD, encoding="utf-8")
    (story_dir / "epic-12-infra.md").write_text(_INFRA_EPIC_MD, encoding="utf-8")
    if nfr is not None:
        (story_dir / "non-functional-requirements.md").write_text(nfr, encoding="utf-8")
    return story_dir


# --- AC1: discovery -----------------------------------------------------------


def test_parse_nfr_file_yields_nfr_stories(tmp_path) -> None:
    story_dir = _seed(tmp_path)
    stories = parse_epic_file(story_dir / "non-functional-requirements.md")
    assert [s.id for s in stories] == ["NFR-SEC-001", "NFR-SEC-002", "NFR-PERF-001"]
    sec2 = stories[1]
    assert sec2.title == "Token rotation"
    assert sec2.epic_id == "nfr"
    assert sec2.points == 5
    assert sec2.dependencies == ["NFR-SEC-001", "99.1-001"]
    assert sec2.done is False
    assert stories[2].done is True


def test_discover_all_includes_nfr_stories(tmp_path) -> None:
    _seed(tmp_path)
    ids = {s.id for s in discover_queue("all", tmp_path)}
    assert {"NFR-SEC-001", "NFR-SEC-002", "NFR-PERF-001", "99.1-001", "12.1-001"} <= ids


def test_discover_reads_nfr_file_from_bare_stories_dir(tmp_path) -> None:
    _seed(tmp_path, subdir="stories")
    assert [s.id for s in discover_queue("NFR-SEC-002", tmp_path)] == ["NFR-SEC-002"]


def test_prose_only_nfr_file_yields_no_stories_and_no_error(tmp_path) -> None:
    prose = "# Non-Functional Requirements\n\n## Security\n\n- **No secrets**: never.\n"
    story_dir = _seed(tmp_path, nfr=prose)
    assert parse_epic_file(story_dir / "non-functional-requirements.md") == []
    assert discover_queue("nfr", tmp_path) == []
    assert all(not s.id.startswith("NFR-") for s in discover_queue("all", tmp_path))


def test_repo_without_nfr_file_is_unchanged(tmp_path) -> None:
    _seed(tmp_path, nfr=None)
    assert {s.id for s in discover_queue("all", tmp_path)} == {
        "99.1-001", "99.1-002", "12.1-001",
    }
    assert discover_queue("nfr", tmp_path) == []
    assert discover_queue("NFR-SEC-001", tmp_path) == []


# --- AC3: scope selection -----------------------------------------------------


@pytest.mark.parametrize("scope", ["NFR-SEC-002", "nfr-sec-002", "Nfr-Sec-002"])
def test_nfr_story_scope_is_case_insensitive(tmp_path, scope) -> None:
    _seed(tmp_path)
    queue = discover_queue(scope, tmp_path)
    # The canonical spelling survives — it keys the ledger, registry and branch.
    assert [s.id for s in queue] == ["NFR-SEC-002"]


def test_mixed_nfr_and_numeric_scope(tmp_path) -> None:
    _seed(tmp_path)
    queue = discover_queue("nfr-sec-001, 99.1-002", tmp_path)
    assert [s.id for s in queue] == ["NFR-SEC-001", "99.1-002"]


def test_nfr_keyword_selects_every_nfr_story_only(tmp_path) -> None:
    _seed(tmp_path)
    for scope in ("nfr", "NFR"):
        ids = [s.id for s in discover_queue(scope, tmp_path)]
        # Never the `epic-12-infra.md` stories, though "infra" contains "nfr".
        assert ids == ["NFR-SEC-001", "NFR-SEC-002", "NFR-PERF-001"]


def test_unknown_nfr_id_matches_nothing(tmp_path) -> None:
    _seed(tmp_path)
    assert discover_queue("NFR-SEC-999", tmp_path) == []


def test_epic_scope_never_selects_the_nfr_file(tmp_path) -> None:
    _seed(tmp_path)
    assert {s.id for s in discover_queue("epic-99", tmp_path)} == {"99.1-001", "99.1-002"}


# --- AC4: dependencies across NFR and epic stories ----------------------------


def test_epic_story_depending_on_nfr_story_is_scheduled_after_it(tmp_path) -> None:
    _seed(tmp_path)
    queue = discover_queue("epic-99 nfr", tmp_path)
    by_id = {s.id: s for s in queue}
    assert by_id["99.1-002"].dependencies == ["NFR-SEC-001"]
    cohorts = [[s.id for s in c] for c in compute_cohorts(queue)]
    position = {sid: i for i, cohort in enumerate(cohorts) for sid in cohort}
    assert position["NFR-SEC-001"] < position["99.1-002"]
    # And the reverse edge: NFR-SEC-002 depends on an epic story.
    assert position["99.1-001"] < position["NFR-SEC-002"]
    assert position["NFR-SEC-001"] < position["NFR-SEC-002"]


def test_lowercase_nfr_dependency_resolves_to_canonical_id(tmp_path) -> None:
    epic = _EPIC_MD.replace("**Dependencies**: NFR-SEC-001", "**Dependencies**: nfr-sec-001")
    story_dir = _seed(tmp_path)
    (story_dir / "epic-99-sample.md").write_text(epic, encoding="utf-8")
    by_id = {s.id: s for s in parse_epic_file(story_dir / "epic-99-sample.md")}
    assert by_id["99.1-002"].dependencies == ["NFR-SEC-001"]


# --- AC5: Done markers --------------------------------------------------------


def test_find_epic_file_routes_nfr_id_to_nfr_file(tmp_path) -> None:
    story_dir = _seed(tmp_path)
    nfr_file = story_dir / "non-functional-requirements.md"
    assert find_epic_file("NFR-SEC-002", tmp_path) == nfr_file
    assert find_epic_file("nfr-sec-002", tmp_path) == nfr_file
    assert find_epic_file("99.1-001", tmp_path) == story_dir / "epic-99-sample.md"


def test_find_epic_file_none_for_nfr_id_without_nfr_file(tmp_path) -> None:
    _seed(tmp_path, nfr=None)
    assert find_epic_file("NFR-SEC-002", tmp_path) is None


def test_render_story_done_stamps_nfr_story() -> None:
    text, changed = render_story_done(_NFR_MD, "NFR-SEC-002")
    assert changed is True
    # Inserted right after the NFR heading, not in a neighbouring story.
    assert "### Story NFR-SEC-002: Token rotation\n**Status**: Done\n" in text
    assert "### Story NFR-SEC-001: Secrets never logged\n**Priority**" in text


def test_render_epic_file_marks_nfr_story_done_end_to_end(tmp_path) -> None:
    story_dir = _seed(tmp_path)
    nfr_file = find_epic_file("NFR-SEC-001", tmp_path)
    assert nfr_file is not None
    assert render_epic_file(nfr_file, ["NFR-SEC-001"]) == ["NFR-SEC-001"]
    stories = {s.id: s for s in parse_epic_file(story_dir / "non-functional-requirements.md")}
    assert stories["NFR-SEC-001"].done is True


_NFR_WITH_PROGRESS = _NFR_MD + """
## NFR Progress

| Story | Title | Points | Status |
|-------|-------|--------|--------|
| NFR-SEC-001 | Secrets never logged | 3 | Not Started |
| NFR-SEC-002 | Token rotation | 5 | In Progress |
| NFR-PERF-001 | Fast install | 2 | Done |
"""


def test_render_story_done_updates_nfr_progress_row() -> None:
    text, changed = render_story_done(_NFR_WITH_PROGRESS, "NFR-SEC-002")
    assert changed is True
    assert "| NFR-SEC-002 | Token rotation | 5 | Done |" in text
    # Other rows are untouched.
    assert "| NFR-SEC-001 | Secrets never logged | 3 | Not Started |" in text


def test_progress_row_updated_even_when_status_line_already_done() -> None:
    stale = _NFR_WITH_PROGRESS.replace(
        "| NFR-PERF-001 | Fast install | 2 | Done |",
        "| NFR-PERF-001 | Fast install | 2 | Not Started |",
    )
    text, changed = render_story_done(stale, "NFR-PERF-001")
    assert changed is True
    assert "| NFR-PERF-001 | Fast install | 2 | Done |" in text


def test_render_story_done_noop_when_status_and_progress_both_done() -> None:
    text, changed = render_story_done(_NFR_WITH_PROGRESS, "NFR-PERF-001")
    assert changed is False
    assert text == _NFR_WITH_PROGRESS


def test_progress_table_without_status_column_is_left_alone() -> None:
    no_status = _NFR_MD + "\n## NFR Progress\n\n| Story | Points |\n|---|---|\n| NFR-SEC-001 | 3 |\n"
    text, changed = render_story_done(no_status, "NFR-SEC-001")
    assert changed is True
    assert text.endswith("| NFR-SEC-001 | 3 |\n")


# --- AC6: issue mirror ----------------------------------------------------------


def test_story_render_parses_nfr_docs_with_nfr_epic(tmp_path) -> None:
    _seed(tmp_path)
    docs = {d.story_id: d for d in parse_story_docs(tmp_path)}
    sec2 = docs["NFR-SEC-002"]
    assert (sec2.epic, sec2.feature) == ("nfr", "nfr-sec")
    assert (sec2.points, sec2.risk) == (5, "Medium")
    assert "Story NFR-SEC-002" not in sec2.spec_md  # the header becomes the title
    assert "epic:nfr" in story_labels(sec2.epic, sec2.feature, sec2.points, sec2.risk)


def test_story_inventory_projects_nfr_specs(tmp_path) -> None:
    _seed(tmp_path)
    specs = {s.story_id: s for s in parse_inventory_specs(tmp_path)}
    assert specs["NFR-SEC-001"].epic == "nfr"
    assert specs["NFR-SEC-001"].feature == "nfr-sec"
    assert specs["NFR-SEC-001"].risk == "High"


def test_done_story_ids_includes_done_nfr_stories(tmp_path) -> None:
    _seed(tmp_path)
    assert "NFR-PERF-001" in done_story_ids(tmp_path)
    assert "NFR-SEC-001" not in done_story_ids(tmp_path)


def test_issues_init_mirrors_nfr_stories(tmp_path) -> None:
    _seed(tmp_path)
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    adapter = FakeHost("github")

    result = init_issues(adapter, ledger, root=tmp_path)

    for story_id in ("NFR-SEC-001", "NFR-SEC-002", "NFR-PERF-001"):
        mapping = ledger.inventory_get_mapping(story_id)
        assert mapping is not None and mapping[0] == "github"
    assert "NFR-PERF-001" in result.closed
    titles = {d["title"] for d in adapter.issues.values()}
    assert "NFR-SEC-002: Token rotation" in titles
