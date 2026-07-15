# ABOUTME: Tests for single-sourced shared gate prompts (Story 27.1-003).
# ABOUTME: Duplicated templates exist once under _shared/; the two largest prompts shrank >=40% by bytes.

"""Structural gate for the single-sourced gate-prompt templates.

Story 27.1-003 single-sources the three templates that were duplicated (with
drift) between the ``build-stories`` and ``fix-issue`` skills, and shrinks the
two largest prompts by >=40% bytes without dropping gate criteria, result-block
contracts, or failure-path instructions. These tests pin all three properties:

- each shared template exists exactly once as a regular file under
  ``plugins/autonomous-sdlc/skills/_shared/``; both skill directories resolve
  it via an in-plugin relative symlink (the ``${CLAUDE_PLUGIN_ROOT}`` variable
  is not dereferenced anywhere in dispatched prompts — the controller renders
  prompts inline — so symlinks are the reliable resolution mechanism);
- the shrunk prompts stay under their 60%-of-baseline byte budgets
  (committed baselines: coverage-gate 8866 B, merge-update 9065 B);
- every gate criterion / status enum / result-block marker survives the cut.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT = _REPO_ROOT / "plugins/autonomous-sdlc/skills"
_SHARED = _SKILLS_ROOT / "_shared"

# The three templates the story single-sources (duplicated pre-27.1-003).
_SHARED_TEMPLATES = [
    "coverage-gate-prompt.md",
    "bugfix-agent-prompt.md",
    "doc-update-prompt.md",
]

# The two skill directories that must resolve every shared template.
_CONSUMER_SKILLS = ["build-stories", "fix-issue"]

# 60% of the committed pre-story byte sizes: the >=40% shrink acceptance
# criterion, expressed as a hard ceiling so a regrowth regression fails CI.
_COVERAGE_GATE_MAX_BYTES = int(8866 * 0.6)
_MERGE_UPDATE_MAX_BYTES = int(9065 * 0.6)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1 — each template exists exactly once (canonical file under _shared/),
# and both skills resolve it via an in-plugin symlink.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SHARED_TEMPLATES)
def test_shared_template_is_a_regular_file(name: str) -> None:
    """The canonical copy lives under _shared/ as a real file, not a link."""
    path = _SHARED / name
    assert path.is_file(), f"missing shared template: {path}"
    assert not path.is_symlink(), f"canonical template must not be a symlink: {path}"
    assert _read(path).strip(), f"empty shared template: {path}"


@pytest.mark.parametrize("skill", _CONSUMER_SKILLS)
@pytest.mark.parametrize("name", _SHARED_TEMPLATES)
def test_skill_resolves_shared_template_via_symlink(skill: str, name: str) -> None:
    """Each consumer skill's copy is a symlink resolving to the shared file,
    so existing path references (tests, evals, docs) keep working while the
    content exists exactly once."""
    link = _SKILLS_ROOT / skill / name
    assert link.is_symlink(), f"{link} must be a symlink into _shared/"
    assert link.resolve() == (_SHARED / name).resolve(), (
        f"{link} resolves to {link.resolve()}, expected {_SHARED / name}"
    )
    # A dangling symlink would pass is_symlink(); the content must be readable.
    assert _read(link) == _read(_SHARED / name)


@pytest.mark.parametrize("name", _SHARED_TEMPLATES)
def test_template_content_exists_exactly_once_in_plugin(name: str) -> None:
    """Searching the plugin for each template finds exactly one regular file —
    the _shared/ canonical copy; every other occurrence is a symlink."""
    regular = [
        p
        for p in _SKILLS_ROOT.rglob(name)
        if p.is_file() and not p.is_symlink()
    ]
    assert regular == [_SHARED / name], (
        f"{name}: expected the only regular copy under _shared/, found {regular}"
    )


# ---------------------------------------------------------------------------
# AC2 — the two largest prompts are >=40% smaller by bytes.
# ---------------------------------------------------------------------------


def test_coverage_gate_prompt_shrank_at_least_40_percent() -> None:
    size = (_SHARED / "coverage-gate-prompt.md").stat().st_size
    assert size <= _COVERAGE_GATE_MAX_BYTES, (
        f"coverage-gate-prompt.md is {size} B; budget {_COVERAGE_GATE_MAX_BYTES} B"
    )


def test_merge_update_prompt_shrank_at_least_40_percent() -> None:
    path = _SKILLS_ROOT / "build-stories" / "merge-update-prompt.md"
    size = path.stat().st_size
    assert size <= _MERGE_UPDATE_MAX_BYTES, (
        f"merge-update-prompt.md is {size} B; budget {_MERGE_UPDATE_MAX_BYTES} B"
    )


# ---------------------------------------------------------------------------
# AC2 (preservation) — gate criteria, result-block contracts, and failure-path
# instructions survive the shrink.
# ---------------------------------------------------------------------------


def _coverage_gate() -> str:
    return _read(_SHARED / "coverage-gate-prompt.md")


def test_coverage_gate_keeps_threshold_and_status_enums() -> None:
    text = _coverage_gate()
    assert "{{COVERAGE_THRESHOLD}}" in text
    for line in (
        "COVERAGE_PCT:",
        "TESTS_ADDED:",
        "PR_NUMBER:",
        "PR_URL:",
        "COVERAGE_STATUS: PASS | WARN",
        "SAST_STATUS: CLEAN | WARN | BLOCK | SKIPPED",
        "DEP_SCAN_STATUS: CLEAN | WARN | BLOCK | SKIPPED",
    ):
        assert line in text, f"coverage gate lost output-contract line: {line!r}"


def test_coverage_gate_keeps_security_scan_gate_criteria() -> None:
    """Both Epic-09 scans survive: wrappers, suppression configs, and the
    BLOCK-routes-to-bugfix failure path."""
    text = _coverage_gate()
    assert "scripts/sast-scan.sh" in text
    assert "scripts/osv-scan.sh" in text
    assert ".sast-config.yaml" in text
    assert ".dep-scan-suppressions.yaml" in text
    assert "{{SECURITY_SCAN}}" in text
    lowered = text.lower()
    assert "bugfix" in lowered, "BLOCK verdict must still route to the bugfix loop"


def test_coverage_gate_keeps_result_block_contract() -> None:
    text = _coverage_gate()
    assert RESULT_START_MARKER in text
    assert RESULT_END_MARKER in text
    assert "coverage-agent-response.schema.json" in text
    # The CLEAN/WARN/BLOCK/SKIPPED -> PASS/WARN/FAIL schema mapping is the
    # contract the controller validates against.
    for key in ('"coverage_status"', '"sast_status"', '"dep_scan_status"'):
        assert key in text, f"coverage gate lost result-block key {key}"


def test_coverage_gate_serves_both_orchestrators() -> None:
    """The single-sourced template names both dispatch contexts."""
    text = _coverage_gate()
    assert "{{STORY_ID}}" in text
    assert "{{ISSUE_NUMBER}}" in text


def _merge_update() -> str:
    return _read(_SKILLS_ROOT / "build-stories" / "merge-update-prompt.md")


def test_merge_update_keeps_every_status_enum() -> None:
    text = _merge_update()
    for status in (
        "MERGE_STATUS: SUCCESS",
        "BLOCKED_HIGH_RISK",
        "REBASE_CONFLICT",
        "CONFLICT",
        "FAILED",
        "PR_MISSING",
    ):
        assert status in text, f"merge-update lost status: {status!r}"


def test_merge_update_keeps_high_risk_gate_criteria() -> None:
    """The human-approval gate survives: risk:high label, both approval paths,
    and the ban on bypassing via --admin."""
    text = _merge_update()
    assert "risk:high" in text
    assert "risk-approved" in text
    assert "risk-approver" in text
    assert "--admin" in text


def test_merge_update_keeps_failure_path_instructions() -> None:
    """Rebase-conflict and missing-PR failure paths still stop the agent."""
    text = _merge_update()
    assert "gh pr update-branch" in text
    assert "gh pr view" in text
    lowered = text.lower()
    assert "stop" in lowered
    assert "resume" in lowered, "resume-aware PR reuse instructions must survive"


def test_merge_update_keeps_result_block_contract() -> None:
    text = _merge_update()
    assert RESULT_START_MARKER in text
    assert RESULT_END_MARKER in text
    assert "merge-agent-response.schema.json" in text
    assert "MERGED" in text and "SKIPPED" in text
    for key in ('"merge_status"', '"merge_sha"', '"merged_at"'):
        assert key in text, f"merge-update lost result-block key {key}"


def test_merge_update_keeps_ledger_emit_lines() -> None:
    text = _merge_update()
    assert "sdlc-state-emit.sh" in text
    assert "stage-start" in text
    assert "stage-finish" in text


def _doc_update() -> str:
    return _read(_SHARED / "doc-update-prompt.md")


def test_doc_update_keeps_output_contract() -> None:
    text = _doc_update()
    for status in (
        "DOC_UPDATE_STATUS: UPDATED",
        "DOC_UPDATE_STATUS: NO_CHANGES",
        "DOC_UPDATE_STATUS: FAILED",
        "FILES_UPDATED:",
        "COMMIT_SHA:",
    ):
        assert status in text, f"doc-update lost contract line: {status!r}"


def test_bugfix_template_serves_both_orchestrators() -> None:
    """The merged bugfix template covers both dispatch contexts (story build
    and issue fix) and keeps the review failure path in its step enum."""
    text = _read(_SHARED / "bugfix-agent-prompt.md")
    assert "{{STORY_ID}}" in text
    assert "{{ISSUE_NUMBER}}" in text
    assert "review" in text.lower()
