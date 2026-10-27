# ABOUTME: Story 36.2-001 — the generate-epics NFR template must stay parseable:
# ABOUTME: a file built from it feeds discovery and story_inventory with points+risk.

from __future__ import annotations

import re
from pathlib import Path

from sdlc.discovery import NFR_FILE_NAME, discover_queue
from sdlc.story_inventory import parse_inventory_specs

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The template is the fixture: tests read it straight from the skill, so a
# template edit the parser cannot read fails here instead of on a user's repo.
_RULES = (
    _REPO_ROOT / "plugins" / "autonomous-sdlc" / "skills" / "generate-epics"
    / "generation-rules.md"
)
_TEMPLATE_BLOCK = re.compile(
    r"^## NFR File Template\n.*?^```markdown\n(.*?)^```", re.MULTILINE | re.DOTALL
)
_STORY_HEADING = re.compile(r"^###\s+Story\s+(NFR-[A-Z]+-[0-9]+):", re.MULTILINE)
# The epic-story field lines, each required at the start of its own line —
# `_POINTS` / `_RISK` read nothing else.
_REQUIRED_FIELDS = (
    "**User Story**:",
    "**Priority**:",
    "**Story Points**:",
    "**Acceptance Criteria**:",
    "**Dependencies**:",
    "**Risk Level**:",
)
_POINTS = 3
_RISK = "Medium"


def _template() -> str:
    m = _TEMPLATE_BLOCK.search(_RULES.read_text(encoding="utf-8"))
    assert m, f"no ```markdown block under '## NFR File Template' in {_RULES}"
    return m.group(1)


def _fill(template: str) -> str:
    """A generated NFR file: the template with its placeholders filled in."""
    return (
        template.replace("[N]", str(_POINTS))
        .replace("High / Medium / Low", _RISK)
        .replace("Must Have / Should Have / Could Have", "Must Have")
    )


def _story_sections(text: str) -> dict[str, str]:
    """Each NFR story's id → its body, up to the next heading."""
    sections: dict[str, str] = {}
    parts = _STORY_HEADING.split(text)
    for story_id, body in zip(parts[1::2], parts[2::2]):
        sections[story_id] = re.split(r"^#{1,6}\s", body, maxsplit=1, flags=re.MULTILINE)[0]
    return sections


def _write_nfr(root: Path, text: str) -> None:
    story_dir = root / "docs" / "stories"
    story_dir.mkdir(parents=True)
    (story_dir / NFR_FILE_NAME).write_text(text, encoding="utf-8")


# --- AC1: the template carries the epic-story fields on their own lines --------


def test_every_template_story_carries_the_epic_story_fields() -> None:
    sections = _story_sections(_template())
    assert len(sections) >= 5, "expected one story per NFR area"
    for story_id, body in sections.items():
        lines = body.splitlines()
        for field in _REQUIRED_FIELDS:
            assert any(line.startswith(field) for line in lines), (
                f"{story_id}: no line starting with {field}"
            )


# --- AC2: a file built from the template parses with points and risk -----------


def test_file_from_template_parses_every_story_with_points_and_risk(tmp_path) -> None:
    filled = _fill(_template())
    expected = list(_story_sections(filled))
    _write_nfr(tmp_path, filled)

    queue = discover_queue("nfr", tmp_path)
    assert [s.id for s in queue] == expected
    assert all(s.points == _POINTS for s in queue)

    specs = parse_inventory_specs(tmp_path)
    assert [s.story_id for s in specs] == expected
    assert [(s.points, s.risk) for s in specs] == [(_POINTS, _RISK)] * len(expected)


# --- AC3: the legacy one-line shape is found, with missing fields left missing -


_ONE_LINE_NFR = """\
# Non-Functional Requirements

## Performance Requirements
### Story NFR-PERF-001: Route in under 50 ms
**Priority**: Must Have · **Story Points**: 3 · **Phase**: 1

Requests are routed within 50 ms at p95.
"""


def test_one_line_field_shape_is_found_with_points_and_risk_missing(tmp_path) -> None:
    _write_nfr(tmp_path, _ONE_LINE_NFR)

    assert [s.id for s in discover_queue("nfr", tmp_path)] == ["NFR-PERF-001"]

    [spec] = parse_inventory_specs(tmp_path)
    assert spec.story_id == "NFR-PERF-001"
    # Not lifted from mid-line, and not guessed: missing stays None.
    assert spec.points is None
    assert spec.risk is None


def test_template_story_without_risk_line_reports_risk_missing(tmp_path) -> None:
    filled = _fill(_template())
    without_risk = "\n".join(
        line for line in filled.splitlines() if not line.startswith("**Risk Level**:")
    )
    _write_nfr(tmp_path, without_risk)

    specs = parse_inventory_specs(tmp_path)
    assert specs, "stories must still be found without a risk line"
    assert all(s.points == _POINTS and s.risk is None for s in specs)
