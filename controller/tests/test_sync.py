# ABOUTME: Tests for the Codex mirror sync parity logic (Story 7.4-001).
# ABOUTME: Filesystem-only; no real git or network — runs hermetically.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.sync import (
    SHARED_SKILLS,
    SkillState,
    SyncReport,
    discover_shared_skills,
    parity_report,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed_source(root: Path, skills: dict[str, str]) -> Path:
    """Lay down a source-of-truth `shared-skills/` tree and return its path."""
    src = root / "shared-skills"
    for name, body in skills.items():
        _write(src / f"{name}.md", body)
    return src


def _seed_consumer(root: Path, skills: dict[str, str]) -> Path:
    """Lay down a consumer submodule checkout and return its path."""
    consumer = root / "consumer" / "shared-skills"
    for name, body in skills.items():
        _write(consumer / f"{name}.md", body)
    return consumer


# --- discover_shared_skills -------------------------------------------------


def test_discover_lists_markdown_skills(tmp_path: Path) -> None:
    src = _seed_source(tmp_path, {"roast": "a", "coverage": "b"})
    assert discover_shared_skills(src) == {"coverage": "b", "roast": "a"}


def test_discover_ignores_non_markdown_and_readme(tmp_path: Path) -> None:
    src = _seed_source(tmp_path, {"roast": "a"})
    _write(src / "README.md", "index, not a skill")
    _write(src / "notes.txt", "ignored")
    assert discover_shared_skills(src) == {"roast": "a"}


def test_discover_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover_shared_skills(tmp_path / "nope")


# --- parity_report ----------------------------------------------------------


def test_parity_all_in_sync(tmp_path: Path) -> None:
    skills = {"roast": "x", "coverage": "y"}
    src = _seed_source(tmp_path, skills)
    consumer = _seed_consumer(tmp_path, skills)

    report = parity_report(src, consumer)

    assert isinstance(report, SyncReport)
    assert report.in_sync is True
    assert {s.name: s.state for s in report.skills} == {
        "roast": SkillState.IN_SYNC,
        "coverage": SkillState.IN_SYNC,
    }


def test_parity_detects_drift(tmp_path: Path) -> None:
    src = _seed_source(tmp_path, {"roast": "new body"})
    consumer = _seed_consumer(tmp_path, {"roast": "old body"})

    report = parity_report(src, consumer)

    assert report.in_sync is False
    drifted = {s.name: s.state for s in report.skills}
    assert drifted["roast"] == SkillState.DRIFTED


def test_parity_detects_missing_in_consumer(tmp_path: Path) -> None:
    src = _seed_source(tmp_path, {"roast": "x", "coverage": "y"})
    consumer = _seed_consumer(tmp_path, {"roast": "x"})

    report = parity_report(src, consumer)

    assert report.in_sync is False
    states = {s.name: s.state for s in report.skills}
    assert states["coverage"] == SkillState.MISSING_IN_CONSUMER


def test_parity_detects_extra_in_consumer(tmp_path: Path) -> None:
    src = _seed_source(tmp_path, {"roast": "x"})
    consumer = _seed_consumer(tmp_path, {"roast": "x", "stale": "z"})

    report = parity_report(src, consumer)

    assert report.in_sync is False
    states = {s.name: s.state for s in report.skills}
    assert states["stale"] == SkillState.EXTRA_IN_CONSUMER


def test_parity_skill_names_sorted(tmp_path: Path) -> None:
    skills = {"zeta": "1", "alpha": "2", "mid": "3"}
    src = _seed_source(tmp_path, skills)
    consumer = _seed_consumer(tmp_path, skills)

    report = parity_report(src, consumer)

    assert [s.name for s in report.skills] == ["alpha", "mid", "zeta"]


# --- SHARED_SKILLS manifest -------------------------------------------------


def test_shared_skills_manifest_matches_acceptance_criteria() -> None:
    """The seven Codex extras named in AC #3 are the shared skill set."""
    assert SHARED_SKILLS == (
        "check-releases",
        "coverage",
        "create-issue",
        "create-project-summary-stats",
        "plan-release-update",
        "project-review",
        "roast",
    )


# --- end-to-end sync cycle (DoD: "test sync verified") ----------------------


def test_bump_in_source_propagates_to_consumer(tmp_path: Path) -> None:
    """Bump a skill in source → consumer drifts → re-sync restores parity.

    Models the consumer running `git submodule update --remote`: copying the
    source skill body into the consumer brings the report back to in-sync.
    """
    src = _seed_source(tmp_path, {"roast": "v1"})
    consumer = _seed_consumer(tmp_path, {"roast": "v1"})
    assert parity_report(src, consumer).in_sync is True

    # Bump the skill in the source of truth.
    (src / "roast.md").write_text("v2", encoding="utf-8")
    drifted = parity_report(src, consumer)
    assert drifted.in_sync is False
    assert drifted.skills[0].state is SkillState.DRIFTED

    # Propagate (what `git submodule update --remote` does to the checkout).
    (consumer / "roast.md").write_text("v2", encoding="utf-8")
    assert parity_report(src, consumer).in_sync is True


def test_real_shared_skills_dir_holds_the_seven_extras() -> None:
    """The committed shared-skills/ tree is the single source of truth."""
    repo_root = Path(__file__).resolve().parents[2]
    shared = repo_root / "shared-skills"
    discovered = set(discover_shared_skills(shared))
    assert discovered == set(SHARED_SKILLS)


# --- edge-case tests (gap analysis: Story 7.4-001 QA gate) -------------------


def test_discover_empty_dir_returns_empty_dict(tmp_path: Path) -> None:
    """An empty skills dir (no *.md) is valid — returns {} not an error."""
    empty = tmp_path / "shared-skills"
    empty.mkdir()
    assert discover_shared_skills(empty) == {}


def test_parity_both_empty_dirs_are_in_sync(tmp_path: Path) -> None:
    """Two empty source/consumer dirs vacuously satisfy parity."""
    src = tmp_path / "src"
    consumer = tmp_path / "consumer"
    src.mkdir()
    consumer.mkdir()
    report = parity_report(src, consumer)
    assert report.in_sync is True
    assert report.skills == ()


def test_discover_ignores_nested_subdirectories(tmp_path: Path) -> None:
    """*.md files in sub-directories are NOT picked up (glob is non-recursive)."""
    src = tmp_path / "shared-skills"
    src.mkdir()
    _write(src / "top.md", "skill content")
    nested_dir = src / "subdir"
    nested_dir.mkdir()
    _write(nested_dir / "nested.md", "nested — must be ignored")
    result = discover_shared_skills(src)
    assert "top" in result
    assert "nested" not in result


def test_crlf_vs_lf_are_treated_as_equal(tmp_path: Path) -> None:
    """Python Path.read_text() applies universal-newlines normalization.

    CRLF and LF variants of the same logical text compare equal via the sync
    check — the comparison is text-level (after Python decoding), not raw bytes.
    This is documented behaviour: skill files stored with different line endings
    on different OSes do NOT cause false drift.
    """
    src = tmp_path / "src"
    consumer = tmp_path / "consumer"
    src.mkdir()
    consumer.mkdir()
    lf_body = "# Skill\n\nSome content\n"
    crlf_body = "# Skill\r\n\r\nSome content\r\n"
    _write(src / "roast.md", lf_body)
    _write(consumer / "roast.md", crlf_body)
    report = parity_report(src, consumer)
    # Universal-newlines normalises both to LF on read → treated as IN_SYNC
    assert report.in_sync is True
    states = {s.name: s.state for s in report.skills}
    assert states["roast"] is SkillState.IN_SYNC


def test_parity_source_empty_consumer_has_extra(tmp_path: Path) -> None:
    """Extra skills in consumer (source is empty) are all flagged as EXTRA_IN_CONSUMER."""
    src = tmp_path / "src"
    src.mkdir()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write(consumer / "stale.md", "leftover skill")
    report = parity_report(src, consumer)
    assert report.in_sync is False
    assert report.skills[0].state is SkillState.EXTRA_IN_CONSUMER
    assert report.skills[0].name == "stale"


def test_parity_consumer_empty_source_has_skills(tmp_path: Path) -> None:
    """All source skills are MISSING_IN_CONSUMER when consumer dir is empty."""
    src = tmp_path / "src"
    src.mkdir()
    _write(src / "coverage.md", "cov skill")
    _write(src / "roast.md", "roast skill")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    report = parity_report(src, consumer)
    assert report.in_sync is False
    states = {s.name: s.state for s in report.skills}
    assert all(v is SkillState.MISSING_IN_CONSUMER for v in states.values())


def test_parity_mixed_states_in_one_report(tmp_path: Path) -> None:
    """A single report can contain IN_SYNC, DRIFTED, MISSING, and EXTRA simultaneously."""
    src = _seed_source(
        tmp_path,
        {"alpha": "same", "beta": "v2", "gamma": "only-in-src"},
    )
    consumer = _seed_consumer(
        tmp_path,
        {"alpha": "same", "beta": "v1", "delta": "only-in-consumer"},
    )
    report = parity_report(src, consumer)
    states = {s.name: s.state for s in report.skills}
    assert states["alpha"] is SkillState.IN_SYNC
    assert states["beta"] is SkillState.DRIFTED
    assert states["gamma"] is SkillState.MISSING_IN_CONSUMER
    assert states["delta"] is SkillState.EXTRA_IN_CONSUMER
    assert report.in_sync is False
