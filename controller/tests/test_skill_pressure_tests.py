# ABOUTME: Structural gate for the RED/GREEN skill pressure-tests (Story 26.3-001).
# ABOUTME: Asserts the on-demand eval suite is well-formed and traces to the Epic-26 discipline it proves.

"""Structural validation for the Epic-26 skill pressure-test suite.

The pressure-tests themselves invoke live agents (cost, nondeterminism) via
``claude plugin eval`` and are deliberately **not** wired into CI (Story
26.3-001 AC4). What CI *can* protect cheaply is the suite's *shape*: every case
keeps its scenario, its recorded RED baseline, and its GREEN grader, and each
still traces to the discipline prompt it exists to prove. If a later edit guts a
grader, deletes a baseline, or renames the discipline out from under a case,
these tests fail — the same "evidence before claims" the pipeline applies to
code, applied to its own process documentation.

These tests read static files only; they never dispatch an agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVALS_ROOT = _REPO_ROOT / "plugins/autonomous-sdlc/evals"

# The two pressure-test cases the story ships, each keyed to the discipline
# (and the prompt files) it proves under pressure.
_CASES = ["root-cause-discipline", "finding-verification"]

# The dispatched bugfix prompts that carry the discipline being pressure-tested.
# The discipline is cross-shipped in BOTH skills, so every scenario must name
# both — otherwise the with/without-plugin ablation only exercises one path.
_DISCIPLINE_PROMPTS = [
    "plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md",
    "plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md",
]

# The two non-scenario legs of each case's triple. `prompt.md` gets its own
# nonempty check above; these two carry the recorded RED and the GREEN rubric.
_ARTIFACTS = ["baseline.md", "graders/criteria.md"]


def _case_dir(name: str) -> Path:
    return _EVALS_ROOT / name


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The suite exists and every case carries the three-part harness contract:
# scenario (prompt.md) + recorded RED baseline (baseline.md) + GREEN grader.
# ---------------------------------------------------------------------------


def test_evals_root_exists() -> None:
    """The pressure-test suite lives under the plugin so `claude plugin eval`
    (which discovers `evals/**`) finds it."""
    assert _EVALS_ROOT.is_dir(), f"missing eval suite dir: {_EVALS_ROOT}"


@pytest.mark.parametrize("case", _CASES)
def test_case_has_scenario_baseline_and_grader(case: str) -> None:
    """Each case is the reusable triple: scenario + baseline expectation +
    compliance expectation (26.3-001 AC3)."""
    cdir = _case_dir(case)
    assert (cdir / "prompt.md").is_file(), f"{case}: missing scenario prompt.md"
    assert (cdir / "baseline.md").is_file(), f"{case}: missing recorded RED baseline.md"
    grader = cdir / "graders" / "criteria.md"
    assert grader.is_file(), f"{case}: missing GREEN grader graders/criteria.md"


@pytest.mark.parametrize("case", _CASES)
def test_case_prompt_is_nonempty(case: str) -> None:
    """A scenario with no prompt cannot pressure anything."""
    assert _read(_case_dir(case) / "prompt.md").strip(), f"{case}: empty prompt.md"


# ---------------------------------------------------------------------------
# Case 1 — root-cause discipline (proves Story 26.1-001 under pressure).
# ---------------------------------------------------------------------------


def test_root_cause_prompt_applies_symptom_patch_pressure() -> None:
    """The scenario must actively pressure toward a symptom patch — otherwise a
    passing GREEN run proves nothing (superpowers: watch it fail first)."""
    prompt = _read(_case_dir("root-cause-discipline") / "prompt.md").lower()
    # A named pressure the discipline's rationalization table must resist.
    assert "ci" in prompt
    assert "budget" in prompt or "retry" in prompt or "quick" in prompt


def test_root_cause_grader_scores_investigation_before_fix() -> None:
    """The GREEN rubric rewards a stated root cause reached before any fix."""
    grader = _read(_case_dir("root-cause-discipline") / "graders" / "criteria.md").lower()
    assert "root cause" in grader
    assert "before" in grader
    # It must reject the symptom-patch the RED baseline records.
    assert "symptom" in grader


def test_root_cause_baseline_records_symptom_patch() -> None:
    """The committed RED baseline documents the un-disciplined symptom-patch
    behaviour (26.3-001 AC1)."""
    baseline = _read(_case_dir("root-cause-discipline") / "baseline.md").lower()
    assert "red" in baseline
    assert "symptom" in baseline


# ---------------------------------------------------------------------------
# Case 2 — finding verification (proves Story 26.2-001 under pressure).
# ---------------------------------------------------------------------------


def test_finding_prompt_presents_a_wrong_finding() -> None:
    """The scenario hands the agent a deliberately wrong review finding —
    correct code flagged as buggy (26.3-001 AC2)."""
    prompt = _read(_case_dir("finding-verification") / "prompt.md").lower()
    assert "finding" in prompt
    # The scenario must signal the finding is (verifiably) wrong.
    assert "guard" in prompt or "correct" in prompt or "wrong" in prompt


def test_finding_grader_scores_reasoned_dispute() -> None:
    """The GREEN rubric rewards a reasoned dispute, not blind implementation."""
    grader = _read(_case_dir("finding-verification") / "graders" / "criteria.md").lower()
    assert "disput" in grader
    assert "finding_dispositions" in grader
    # A dispute without technical reasoning is performative — the rubric says so.
    assert "reasoning" in grader


def test_finding_baseline_records_blind_implementation() -> None:
    """The committed RED baseline documents blind implementation of the wrong
    finding (26.3-001 AC2)."""
    baseline = _read(_case_dir("finding-verification") / "baseline.md").lower()
    assert "red" in baseline
    assert "implement" in baseline


# ---------------------------------------------------------------------------
# Traceability — every case names the discipline prompt it proves, so a rename
# or deletion of the discipline surfaces here instead of silently orphaning the
# pressure-test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES)
def test_case_traces_to_a_real_discipline_prompt(case: str) -> None:
    """Each case's prompt references at least one dispatched discipline prompt
    that actually exists on disk."""
    prompt = _read(_case_dir(case) / "prompt.md")
    referenced = [p for p in _DISCIPLINE_PROMPTS if p in prompt]
    assert referenced, f"{case}: prompt names no known discipline prompt file"
    for rel in referenced:
        assert (_REPO_ROOT / rel).is_file(), f"{case}: references missing prompt {rel}"


# ---------------------------------------------------------------------------
# The harness README documents the runner decision and the CI stance so the
# methodology is reusable (AC3) and its on-demand-only nature is explicit (AC4).
# ---------------------------------------------------------------------------


def test_readme_exists() -> None:
    assert (_EVALS_ROOT / "README.md").is_file(), "missing evals/README.md harness doc"


def test_readme_names_claude_plugin_eval_as_runner() -> None:
    """AC3: `claude plugin eval` was evaluated and chosen as the runner before
    building anything custom — the README says so."""
    readme = _read(_EVALS_ROOT / "README.md").lower()
    assert "claude plugin eval" in readme
    # The RED/GREEN arms come from the with/without-plugin ablation.
    assert "ablation" in readme


def test_readme_marks_suite_as_on_demand_not_ci() -> None:
    """AC4: explicitly not wired into the PR gate; CI integration deferred to
    Epic-18."""
    readme = _read(_EVALS_ROOT / "README.md").lower()
    assert "on-demand" in readme or "on demand" in readme
    assert "not" in readme and ("ci" in readme or "pr gate" in readme)
    assert "epic-18" in readme or "epic 18" in readme


def test_readme_documents_adding_a_case() -> None:
    """AC3: the next case must cost an hour, not a design session — the README
    spells out the reusable structure."""
    readme = _read(_EVALS_ROOT / "README.md").lower()
    assert "add" in readme
    for part in ("prompt.md", "baseline.md", "criteria.md"):
        assert part in readme, f"README does not document the {part} step"


def test_readme_cites_superpowers_pattern_source() -> None:
    """AC3: the reusable methodology is not invented here — the README names its
    provenance (obra/superpowers "TDD for skills") so the pattern is auditable
    and reusable, not folklore."""
    readme = _read(_EVALS_ROOT / "README.md").lower()
    assert "superpowers" in readme


# ---------------------------------------------------------------------------
# The other two legs of the triple must exist AND carry content. `prompt.md`
# has its own nonempty guard above; a gutted baseline or empty grader would
# otherwise slip past the shape checks (an empty file is still a file).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("artifact", _ARTIFACTS)
def test_case_artifact_is_nonempty(case: str, artifact: str) -> None:
    """A recorded baseline or grader with no body cannot prove or grade
    anything — the RED evidence and GREEN rubric must actually be written."""
    assert _read(_case_dir(case) / artifact).strip(), f"{case}: empty {artifact}"


# ---------------------------------------------------------------------------
# RED/GREEN linkage — the whole method is "GREEN is meaningful only if it beats
# the recorded RED". That only holds if baseline and grader cross-reference each
# other; a broken link silently decouples the two arms.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES)
def test_case_baseline_links_forward_to_grader(case: str) -> None:
    """The RED baseline must point to the GREEN rubric that supersedes it, so a
    reader lands on the compliance criteria, not a dead end."""
    baseline = _read(_case_dir(case) / "baseline.md")
    assert "graders/criteria.md" in baseline, f"{case}: baseline names no grader"


@pytest.mark.parametrize("case", _CASES)
def test_case_grader_links_baseline_for_red_comparison(case: str) -> None:
    """The GREEN rubric must reference the recorded RED baseline: a PASS only
    counts if it *beats* that RED arm. Drop the link and the ablation loses its
    reference point."""
    grader = _read(_case_dir(case) / "graders" / "criteria.md")
    assert "baseline.md" in grader, f"{case}: grader names no RED baseline"


@pytest.mark.parametrize("case", _CASES)
def test_case_grader_is_bidirectional(case: str) -> None:
    """A rubric that only lists what PASSES cannot reject the RED behaviour. Each
    grader must both reward the discipline (PASS) and reject its violation
    (FAIL) — otherwise the RED arm could score GREEN."""
    grader = _read(_case_dir(case) / "graders" / "criteria.md").lower()
    assert "pass" in grader, f"{case}: grader states no PASS criteria"
    assert "fail" in grader, f"{case}: grader states no FAIL criteria"


# ---------------------------------------------------------------------------
# Cross-shipping parity — the discipline ships in BOTH the build-stories and
# fix-issue skills. Each scenario must exercise both, or a regression in the
# un-referenced copy would go untested by the ablation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES)
def test_case_references_both_discipline_prompt_variants(case: str) -> None:
    """Every scenario names both cross-shipped discipline prompts, and both
    exist on disk — so neither copy can drift out from under the pressure-test."""
    prompt = _read(_case_dir(case) / "prompt.md")
    for rel in _DISCIPLINE_PROMPTS:
        assert rel in prompt, f"{case}: prompt does not reference {rel}"
        assert (_REPO_ROOT / rel).is_file(), f"{case}: references missing prompt {rel}"
