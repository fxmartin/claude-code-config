# ABOUTME: Tests for root-cause-first bugfix discipline (Story 26.1-001).
# ABOUTME: Prompt files demand investigation-before-fix; the rendered controller prompt carries root_cause.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import render_bugfix_prompt
from sdlc.cohort import Story

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The two dispatched bugfix prompts named by the story's acceptance criteria.
_PROMPT_FILES = [
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md",
    _REPO_ROOT / "plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md",
]

# The evasions the rationalization table must name and refuse (26.1-001 AC1).
# Lower-case: matched against the lower-cased prompt text.
_RATIONALIZATIONS = [
    "the fix is obvious",
    "see if ci passes",
    "retry budget is low",
]


def _story() -> Story:
    return Story(
        "26.1-001", "root-cause discipline", "26", "agent-process-discipline",
        "docs/stories/epic-26.md", "Must", 3, "py", [], False,
    )


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_requires_root_cause_before_fix(path: Path) -> None:
    """Both bugfix prompt files state the iron law: no fix without a root cause."""
    text = path.read_text(encoding="utf-8")
    assert "NO FIX WITHOUT A ROOT CAUSE" in text


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_carries_rationalization_table(path: Path) -> None:
    """Both prompt files name the evasions to refuse in a rationalization table."""
    text = path.read_text(encoding="utf-8")
    assert "Rationalization" in text
    lowered = text.lower()
    for evasion in _RATIONALIZATIONS:
        assert evasion in lowered, f"{path.name} missing rationalization: {evasion!r}"


@pytest.mark.parametrize("path", _PROMPT_FILES, ids=lambda p: p.parent.name)
def test_prompt_file_forbids_symptom_restatement(path: Path) -> None:
    """The ROOT_CAUSE the agent reports must explain the defect, not the symptom."""
    text = path.read_text(encoding="utf-8")
    assert "not a restatement of the symptom" in text


def test_build_stories_result_example_includes_root_cause() -> None:
    """The build-stories result-block example shows the required root_cause key."""
    text = _PROMPT_FILES[0].read_text(encoding="utf-8")
    assert '"root_cause"' in text


def test_rendered_bugfix_prompt_requires_root_cause_field() -> None:
    """The controller-rendered prompt (dispatched to every harness — Claude and
    Codex alike, so the discipline is single-source) demands root_cause in its
    schema-derived skeleton."""
    prompt = render_bugfix_prompt(_story(), "build", "boom")
    assert '"root_cause"' in prompt


def test_rendered_bugfix_prompt_states_root_cause_first_discipline() -> None:
    """The rendered prompt orders investigation before any fix attempt."""
    prompt = render_bugfix_prompt(_story(), "build", "boom")
    assert "root cause" in prompt.lower()
    assert "before" in prompt.lower()
    # The field is a diagnosis, not an echo of the failure output.
    assert "not a restatement of the symptom" in prompt


def test_rendered_bugfix_prompt_keeps_wrapper_contract() -> None:
    """The discipline addition must not disturb the result-wrapper contract."""
    from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

    prompt = render_bugfix_prompt(_story(), "build", "boom")
    assert RESULT_START_MARKER in prompt
    assert RESULT_END_MARKER in prompt


@pytest.mark.parametrize("evasion", _RATIONALIZATIONS)
def test_rendered_bugfix_prompt_refuses_each_rationalization(evasion: str) -> None:
    """The rendered controller prompt — not just the .md files — names and
    refuses every shortcut in the rationalization table (26.1-001 AC1), so the
    discipline reaches whichever harness the orchestrator dispatches."""
    prompt = render_bugfix_prompt(_story(), "build", "boom").lower()
    assert evasion in prompt, f"rendered prompt missing rationalization: {evasion!r}"


def test_rendered_bugfix_prompt_investigate_precedes_fix_ordering() -> None:
    """Investigation is ordered strictly before any fix attempt: the 'investigate
    ... BEFORE attempting any fix' clause appears ahead of the fix instruction."""
    prompt = render_bugfix_prompt(_story(), "build", "boom")
    lowered = prompt.lower()
    investigate_at = lowered.find("investigate the root cause")
    fix_at = lowered.find("fix where possible")
    assert investigate_at != -1
    assert fix_at != -1
    assert investigate_at < fix_at


def test_rendered_bugfix_prompt_carries_exact_failure_context() -> None:
    """The rendered prompt threads the concrete failed stage and failure text
    through unaltered, so the agent diagnoses the actual defect it was handed."""
    prompt = render_bugfix_prompt(_story(), "coverage", "AssertionError: x != y")
    assert "Stage 'coverage' failed" in prompt
    assert "Failure: AssertionError: x != y" in prompt


def test_rendered_bugfix_prompt_embeds_conventional_commit_header() -> None:
    """The bugfix commit header stays conventional-commit-compliant and carries
    the story id, unchanged by the root-cause discipline addition."""
    prompt = render_bugfix_prompt(_story(), "build", "boom")
    assert "fix(agent-process-discipline):" in prompt
    assert "(#26.1-001)" in prompt
