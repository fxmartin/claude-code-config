# ABOUTME: Tests for review-finding verification discipline (Story 26.2-001).
# ABOUTME: Bugfix prompts demand per-finding verification + dispute channel; disputes surface in the ledger.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import BuildOptions, Ledger, _run_bugfix, render_bugfix_prompt
from sdlc.cohort import Story
from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.dispatch import AgentResult

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The two dispatched bugfix prompts named by the story's acceptance criteria.
_PROMPT_FILES = [
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md",
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md",
]


def _story() -> Story:
    return Story(
        "26.2-001", "verify review findings", "26", "agent-process-discipline",
        "docs/stories/epic-26.md", "Should", 3, "py", [], False,
    )


# ---------------------------------------------------------------------------
# AC1 — the reception discipline lives in both dispatched prompt files.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_states_findings_are_claims_not_orders(path: Path) -> None:
    """A review finding is a claim to verify, never an order to implement."""
    text = path.read_text(encoding="utf-8").lower()
    assert "claims, not orders" in text


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_carries_reception_sequence(path: Path) -> None:
    """The receiving-code-review reception sequence is spelled out verbatim."""
    text = path.read_text(encoding="utf-8").lower()
    assert "read → restate → verify → evaluate → respond → implement" in text


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_forbids_performative_agreement(path: Path) -> None:
    """Implementing a finding without verifying it is forbidden as performative."""
    text = path.read_text(encoding="utf-8").lower()
    assert "performative" in text
    # The verification must precede implementation, not follow it.
    assert "before implementing" in text or "before you implement" in text


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_documents_dispute_channel(path: Path) -> None:
    """Both prompts document the structured dispute channel + disposition field."""
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "dispute" in lowered
    assert "finding_dispositions" in text


# ---------------------------------------------------------------------------
# The controller-rendered prompt (dispatched to every harness — single source).
# ---------------------------------------------------------------------------

def test_rendered_bugfix_prompt_requires_finding_verification() -> None:
    """The rendered prompt demands per-finding verification before implementing."""
    prompt = render_bugfix_prompt(_story(), "review", "finding: null deref at L42")
    lowered = prompt.lower()
    assert "claims, not orders" in lowered
    assert "verify" in lowered
    assert "dispute" in lowered


def test_rendered_bugfix_prompt_names_disposition_field() -> None:
    """The rendered prompt names finding_dispositions so a dispute is structured."""
    prompt = render_bugfix_prompt(_story(), "review", "finding: null deref at L42")
    assert "finding_dispositions" in prompt


def test_rendered_bugfix_prompt_keeps_wrapper_contract() -> None:
    """The reception discipline must not disturb the result-wrapper contract."""
    prompt = render_bugfix_prompt(_story(), "review", "boom")
    assert RESULT_START_MARKER in prompt
    assert RESULT_END_MARKER in prompt
    # The root-cause discipline (26.1-001) must still be present.
    assert "not a restatement of the symptom" in prompt


# ---------------------------------------------------------------------------
# AC3/AC4 — a disputed finding surfaces in the ledger and is not reported fixed.
# ---------------------------------------------------------------------------

def _bugfix_response(finding_dispositions: list[dict]) -> dict:
    """A bugfix response for the wrong-finding scenario (nothing to fix)."""
    return {
        "failure_category": "TEST_BUG",
        "root_cause": "the review finding misread a guarded access; the code is correct",
        "fix_status": "N/A",
        "tests_passing": True,
        "bugs_fixed": 0,
        "tests_fixed": 0,
        "finding_dispositions": finding_dispositions,
    }


def _dispatch_returning(data: dict):
    def _dispatch(agent_type, prompt, story=None, **kwargs):  # noqa: ANN001
        return AgentResult(agent_type=agent_type, data=data, raw="")
    return _dispatch


def test_disputed_finding_surfaces_in_ledger_and_is_not_fixed(tmp_path) -> None:
    """The acceptance test: a deliberately wrong finding is disputed, surfaced, not fixed.

    A reviewer flags correct code as buggy. The bugfix agent verifies it against
    the code, refutes it, and reports a disputed disposition — the loop must
    surface the dispute (recent_events) and must NOT report the finding as fixed.
    """
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-26", "sequential")
    story = _story()
    ledger.story_upsert(
        run_id, story.id, "26", story.title, "Should", 3, "py", "", None, "IN_PROGRESS",
    )
    opts = BuildOptions(scope="epic-26", skip_preflight=True, sequential=True)

    response = _bugfix_response(
        [
            {
                "finding": "null deref at line 42",
                "disposition": "disputed",
                "reasoning": "line 42 is guarded by `if node is not None` on line 40; no deref is reachable",
            }
        ]
    )
    fixed = _run_bugfix(
        story, "review", "REVIEW FINDING: null deref at line 42",
        opts, ledger, run_id, _dispatch_returning(response),
    )

    # The finding was wrong: nothing was fixed, and the loop says so.
    assert fixed is False

    events = ledger.recent_events(run_id, limit=20)
    disputes = [e for e in events if "disput" in e["message"].lower()]
    assert disputes, "a disputed finding must surface as a ledger event"
    surfaced = disputes[-1]
    assert surfaced["level"] == "warn"
    # The finding text and its reasoning are both visible, not swallowed.
    assert "line 42" in surfaced["message"]
    assert "guarded" in surfaced["message"]


def test_implemented_finding_does_not_surface_a_dispute(tmp_path) -> None:
    """An implemented finding is not noise: it emits no dispute event."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-26", "sequential")
    story = _story()
    ledger.story_upsert(
        run_id, story.id, "26", story.title, "Should", 3, "py", "", None, "IN_PROGRESS",
    )
    opts = BuildOptions(scope="epic-26", skip_preflight=True, sequential=True)

    response = {
        "failure_category": "CODE_BUG",
        "root_cause": "off-by-one in the cursor increment",
        "fix_status": "FIXED",
        "tests_passing": True,
        "bugs_fixed": 1,
        "tests_fixed": 1,
        "finding_dispositions": [
            {"finding": "off-by-one in cursor", "disposition": "implemented"}
        ],
    }
    fixed = _run_bugfix(
        story, "review", "REVIEW FINDING: off-by-one in cursor",
        opts, ledger, run_id, _dispatch_returning(response),
    )
    assert fixed is True
    events = ledger.recent_events(run_id, limit=20)
    assert not [e for e in events if "disput" in e["message"].lower()]


def test_bugfix_without_findings_surfaces_nothing(tmp_path) -> None:
    """A plain build-failure bugfix (no findings) emits no dispute event."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-26", "sequential")
    story = _story()
    ledger.story_upsert(
        run_id, story.id, "26", story.title, "Should", 3, "py", "", None, "IN_PROGRESS",
    )
    opts = BuildOptions(scope="epic-26", skip_preflight=True, sequential=True)

    response = {
        "failure_category": "TEST_FAILURE",
        "root_cause": "assertion compared bytes to str",
        "fix_status": "FIXED",
        "tests_passing": True,
        "bugs_fixed": 1,
        "tests_fixed": 1,
    }
    _run_bugfix(
        story, "build", "boom",
        opts, ledger, run_id, _dispatch_returning(response),
    )
    events = ledger.recent_events(run_id, limit=20)
    assert not [e for e in events if "disput" in e["message"].lower()]
