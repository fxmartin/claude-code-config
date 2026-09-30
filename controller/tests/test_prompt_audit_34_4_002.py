# ABOUTME: Keep-list and obsolete-pattern guard for the Story 34.4-002 prompt audit.
# ABOUTME: Pins incident-driven prompt rules and forbids previous-generation steering patterns.

"""Story 34.4-002: the prompt audit removed no rule, so this file pins the
keep-list (each rule tied to its incident in
``docs/optimization/PROMPT-AUDIT-2026-09.md``) and guards against the obsolete
patterns the audit searched for being introduced later.
"""

from __future__ import annotations

import pytest

from sdlc import build, fix_issue
from sdlc.contracts import _result_wrapper
from sdlc.fix_issue import FixIssue
from sdlc.issue_host import CR_NONE

_STORY = build.Story(
    "99.1-001", "Story 99.1-001", "99", "x", "epic-x.md", "P1", 1, "py", [], False
)
_ISSUE = FixIssue(38, "Bug", "b", "open", (), ())
_SCHEMA = "merge-agent-response.schema.json"

# Patterns written for the previous model generation (audit F-16 / clean checks).
_OBSOLETE = (
    "think step by step",
    "<thinking>",
    "<scratchpad>",
    "do not reason",
    "tool_choice",
    "prefill",
)


def _all_prompts() -> list[str]:
    return [
        _result_wrapper(_SCHEMA),
        build.render_build_prompt(_STORY, build.BuildOptions(scope="epic-99")),
        build.render_coverage_prompt(_STORY, build.BuildOptions(scope="epic-99")),
        build.render_review_prompt(_STORY, 7),
        build.render_merge_prompt(_STORY, 7),
        build.render_bugfix_prompt(_STORY, "build", "boom"),
        fix_issue.render_investigation_prompt(_ISSUE),
        fix_issue.render_merge_prompt(_ISSUE, 5),
        fix_issue.render_bugfix_prompt(_ISSUE, {}, "build", "boom"),
    ]


@pytest.mark.parametrize("pattern", _OBSOLETE)
def test_no_previous_generation_steering_patterns(pattern: str) -> None:
    for prompt in _all_prompts():
        assert pattern not in prompt.lower()


def test_wrapper_keeps_b8fdbc71_session_model_rule() -> None:
    wrapper = _result_wrapper(_SCHEMA)
    for phrase in ("one-shot headless", "scheduled wakeup", "blocking foreground"):
        assert phrase in wrapper


def test_wrapper_keeps_exact_key_and_final_marker_rules() -> None:
    wrapper = _result_wrapper(_SCHEMA)
    assert "nothing after the closing marker" in wrapper
    assert "Use these exact keys" in wrapper


def test_merge_prompts_keep_740_no_pipeline_wait_rule() -> None:
    assert "Do not poll, sleep, or wait" in build.render_merge_prompt(
        _STORY, 7, ci_status=CR_NONE
    )
    assert "Do not poll, sleep, or wait" in fix_issue.render_merge_prompt(
        _ISSUE, 5, ci_status=CR_NONE
    )


def test_merge_prompts_keep_738_no_rebase_rule() -> None:
    for prompt in (build.render_merge_prompt(_STORY, 7), fix_issue.render_merge_prompt(_ISSUE, 5)):
        assert "do not rebase" in prompt
        assert "never a rebase" in prompt


def test_build_prompts_keep_590_no_stash_rule() -> None:
    build_prompt = build.render_build_prompt(_STORY, build.BuildOptions(scope="epic-99"))
    assert "Never run `git stash`" in build_prompt


def test_bugfix_prompt_keeps_epic_26_pressure_test_rules() -> None:
    prompt = build.render_bugfix_prompt(_STORY, "build", "boom")
    assert "Investigate the root cause BEFORE attempting any fix" in prompt
    assert "Refuse the shortcuts" in prompt
    assert "never agree performatively" in prompt
    assert "finding_dispositions" in prompt


def test_review_prompts_keep_do_not_trust_rule() -> None:
    assert "Do not trust the implementer's report" in build.render_review_prompt(_STORY, 7)
    assert "Do NOT trust the implementer's report" in fix_issue.render_review_prompt(_ISSUE, 5)
