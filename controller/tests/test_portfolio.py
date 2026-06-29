# ABOUTME: Tests for the all-epics/all-stories portfolio view (Story 22.6-001).
# ABOUTME: Pure projection of story_inventory rows → grouped-by-epic panel data.

from __future__ import annotations

from sdlc.harness import DEFAULT_HARNESS
from sdlc.portfolio import portfolio_view


def _row(story_id, epic, **kw):
    base = {
        "story_id": story_id,
        "epic": epic,
        "feature": story_id.split("-", 1)[0],
        "title": f"title {story_id}",
        "points": 3,
        "risk": "Medium",
        "status": None,
        "owner": None,
        "human_status": None,
        "host": None,
        "issue_ref": None,
        "harness": None,
    }
    base.update(kw)
    return base


def test_empty_inventory_is_unavailable() -> None:
    view = portfolio_view([])
    assert view["available"] is False
    assert view["epics"] == []
    assert view["total"] == 0


def test_groups_stories_by_epic() -> None:
    rows = [
        _row("22.1-001", "22"),
        _row("22.1-002", "22"),
        _row("13.2-001", "13"),
    ]
    view = portfolio_view(rows)
    assert view["available"] is True
    assert view["total"] == 3
    epics = {e["epic"]: e for e in view["epics"]}
    assert set(epics) == {"22", "13"}
    assert epics["22"]["count"] == 2
    assert [s["story_id"] for s in epics["22"]["stories"]] == ["22.1-001", "22.1-002"]


def test_epics_sorted_numerically_not_lexically() -> None:
    rows = [_row("13.1-001", "13"), _row("4.1-001", "4"), _row("22.1-001", "22")]
    view = portfolio_view(rows)
    assert [e["epic"] for e in view["epics"]] == ["4", "13", "22"]


def test_status_defaults_to_todo_when_unbuilt() -> None:
    view = portfolio_view([_row("22.1-001", "22", status=None)])
    assert view["epics"][0]["stories"][0]["status"] == "TODO"


def test_cached_status_and_owner_are_surfaced() -> None:
    rows = [_row("22.1-001", "22", status="DONE", owner="fxmartin")]
    story = portfolio_view(rows)["epics"][0]["stories"][0]
    assert story["status"] == "DONE"
    assert story["owner"] == "fxmartin"


def test_harness_defaults_to_builtin_when_unset() -> None:
    story = portfolio_view([_row("22.1-001", "22", harness=None)])["epics"][0][
        "stories"
    ][0]
    assert story["harness"] == DEFAULT_HARNESS


def test_harness_summary_is_surfaced_verbatim() -> None:
    story = portfolio_view([_row("13.2-001", "13", harness="codex")])["epics"][0][
        "stories"
    ][0]
    assert story["harness"] == "codex"


def test_per_epic_harness_rollup_counts_most_used_first() -> None:
    rows = [
        _row("13.1-001", "13", harness="codex"),
        _row("13.1-002", "13", harness="codex"),
        _row("13.2-001", "13", harness="codex"),
        _row("13.2-002", "13", harness="codex"),
        _row("13.3-001", "13", harness="claude"),
    ]
    rollup = portfolio_view(rows)["epics"][0]["harness_rollup"]
    assert rollup == [
        {"harness": "codex", "count": 4},
        {"harness": "claude", "count": 1},
    ]


def test_rollup_ties_break_alphabetically() -> None:
    rows = [
        _row("13.1-001", "13", harness="qwen"),
        _row("13.2-001", "13", harness="codex"),
    ]
    rollup = portfolio_view(rows)["epics"][0]["harness_rollup"]
    assert rollup == [
        {"harness": "codex", "count": 1},
        {"harness": "qwen", "count": 1},
    ]


def test_rollup_folds_unset_harness_into_default() -> None:
    rows = [
        _row("13.1-001", "13", harness=None),
        _row("13.2-001", "13", harness=DEFAULT_HARNESS),
    ]
    rollup = portfolio_view(rows)["epics"][0]["harness_rollup"]
    assert rollup == [{"harness": DEFAULT_HARNESS, "count": 2}]
