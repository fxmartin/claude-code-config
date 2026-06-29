# ABOUTME: Build the all-epics/all-stories portfolio view from the inventory cache.
# ABOUTME: Story 22.6-001 — pure projection of story_inventory rows → grouped-by-epic panel data.

from __future__ import annotations

from sdlc.harness import DEFAULT_HARNESS

__all__ = ["portfolio_view"]

# A story with no cached execution status reads as TODO in the panel — the
# inventory `status` is sync/build-populated, so an unbuilt story is simply
# "not started" rather than blank.
_DEFAULT_STATUS = "TODO"


def _epic_sort_key(epic: str) -> tuple[int, int, str]:
    """Sort epics numerically when the id is an int, else lexically (ints first)."""
    try:
        return (0, int(epic), "")
    except (TypeError, ValueError):
        return (1, 0, epic or "")


def _harness_label(value: str | None) -> str:
    """The harness badge label: the cached summary, or the built-in default.

    The inventory ``harness`` column is the derived per-story harness summary
    (Epic-20 20.2-002), populated by sync/build; until a story is synced it is
    NULL, so the panel falls back to the default harness exactly as the state
    view does (COALESCE to ``claude``).
    """
    return value or DEFAULT_HARNESS


def _rollup(harnesses: list[str]) -> list[dict]:
    """Count stories per harness within an epic, most-used first (Story 22.6-001).

    Ties break alphabetically so the roll-up is deterministic, e.g.
    ``Epic-13: 4 on codex, 1 on claude``.
    """
    counts: dict[str, int] = {}
    for harness in harnesses:
        counts[harness] = counts.get(harness, 0) + 1
    return [
        {"harness": harness, "count": count}
        for harness, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def portfolio_view(rows: list[dict]) -> dict:
    """Group inventory rows into the all-epics/all-stories portfolio panel data.

    Pure: ``story_inventory`` rows → ``{"available", "epics", "total"}`` where
    each epic carries its stories (status + owner + harness badge, in input
    order) and a per-harness roll-up. ``available`` is False on an empty
    inventory so the client renders the "run sync first" empty state.
    Host-agnostic — it shows whatever the inventory holds, GitHub or GitLab.
    """
    by_epic: dict[str, list[dict]] = {}
    for row in rows:
        epic = row.get("epic") or "?"
        by_epic.setdefault(epic, []).append(
            {
                "story_id": row.get("story_id"),
                "feature": row.get("feature"),
                "title": row.get("title"),
                "points": row.get("points"),
                "risk": row.get("risk"),
                "status": row.get("status") or _DEFAULT_STATUS,
                "owner": row.get("owner"),
                "human_status": row.get("human_status"),
                "harness": _harness_label(row.get("harness")),
                "host": row.get("host"),
                "issue_ref": row.get("issue_ref"),
            }
        )
    epics = [
        {
            "epic": epic,
            "stories": by_epic[epic],
            "harness_rollup": _rollup([s["harness"] for s in by_epic[epic]]),
            "count": len(by_epic[epic]),
        }
        for epic in sorted(by_epic, key=_epic_sort_key)
    ]
    return {"available": bool(rows), "epics": epics, "total": len(rows)}
