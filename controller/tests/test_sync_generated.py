# ABOUTME: Tests for the cross-harness generated-skill parity gate (Story 20.4-003).
# ABOUTME: Verifies committed skill bodies match what their neutral source generates.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.skill_format import parse_neutral_skill, render_body
from sdlc.skill_generator import PIPELINE_SKILLS, generate_claude_skill
from sdlc.sync import (
    SHARED_SKILLS,
    GeneratedParityReport,
    GeneratedState,
    discover_neutral_skills,
    discover_pipeline_sources,
    generated_parity_report,
    pipeline_parity_report,
    write_generated_skills,
    write_pipeline_skills,
)

# The committed shared-skills tree (source of truth) two levels up from tests/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED_DIR = _REPO_ROOT / "shared-skills"
_NEUTRAL_DIR = _SHARED_DIR / "neutral"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed_neutral(root: Path, sources: dict[str, str]) -> Path:
    """Lay down a neutral/ sources tree (`<name>.skill.md`) and return its path."""
    neutral = root / "neutral"
    neutral.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        _write(neutral / f"{name}.skill.md", text)
    return neutral


def _seed_generated(root: Path, bodies: dict[str, str]) -> Path:
    """Lay down a committed generated tree (`<name>.md`) and return its path."""
    gen = root / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        _write(gen / f"{name}.md", body)
    return gen


# A minimal but valid neutral source: frontmatter + a body with one placeholder.
_DEMO_SRC = (
    "---\n"
    "name: demo\n"
    "description: Use when demoing the parity gate.\n"
    "---\n\n"
    "Do the demo work.\n\n"
    "{{ARGUMENTS}}\n"
)


def _demo_body(harness: str = "claude") -> str:
    return render_body(parse_neutral_skill(_DEMO_SRC), harness)


# --- discover_neutral_skills ------------------------------------------------


def test_discover_neutral_lists_skill_sources(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    discovered = discover_neutral_skills(neutral)
    assert set(discovered) == {"demo"}
    assert discovered["demo"] == _DEMO_SRC


def test_discover_neutral_ignores_plain_md(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    _write(neutral / "README.md", "not a neutral source")
    assert set(discover_neutral_skills(neutral)) == {"demo"}


def test_discover_neutral_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover_neutral_skills(tmp_path / "nope")


# --- generated_parity_report ------------------------------------------------


def test_parity_in_sync(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = _seed_generated(tmp_path, {"demo": _demo_body()})

    report = generated_parity_report(neutral, gen)

    assert isinstance(report, GeneratedParityReport)
    assert report.in_sync is True
    assert {s.name: s.state for s in report.skills} == {"demo": GeneratedState.IN_SYNC}


def test_parity_detects_drift_with_diff(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = _seed_generated(tmp_path, {"demo": "hand-edited body that drifted\n"})

    report = generated_parity_report(neutral, gen)

    assert report.in_sync is False
    drifted = next(s for s in report.skills if s.name == "demo")
    assert drifted.state is GeneratedState.DRIFTED
    # The diff shows both the committed and the regenerated content.
    assert "drifted" in drifted.diff
    assert "regenerated" in drifted.diff


def test_parity_detects_missing_generated(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = _seed_generated(tmp_path, {})  # no committed body

    report = generated_parity_report(neutral, gen)

    assert report.in_sync is False
    assert report.skills[0].state is GeneratedState.MISSING


def test_parity_detects_orphan_generated(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {})  # no neutral source
    gen = _seed_generated(tmp_path, {"stale": "left behind\n"})

    report = generated_parity_report(neutral, gen)

    assert report.in_sync is False
    assert report.skills[0].state is GeneratedState.ORPHAN
    assert report.skills[0].name == "stale"


def test_parity_skill_names_sorted(tmp_path: Path) -> None:
    sources = {n: _DEMO_SRC.replace("demo", n) for n in ("zeta", "alpha", "mid")}
    neutral = _seed_neutral(tmp_path, sources)
    bodies = {n: render_body(parse_neutral_skill(s), "claude") for n, s in sources.items()}
    gen = _seed_generated(tmp_path, bodies)

    report = generated_parity_report(neutral, gen)

    assert [s.name for s in report.skills] == ["alpha", "mid", "zeta"]
    assert report.in_sync is True


def test_parity_missing_neutral_dir_raises(tmp_path: Path) -> None:
    gen = _seed_generated(tmp_path, {"demo": _demo_body()})
    with pytest.raises(FileNotFoundError):
        generated_parity_report(tmp_path / "nope", gen)


def test_parity_missing_generated_dir_raises(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    with pytest.raises(FileNotFoundError):
        generated_parity_report(neutral, tmp_path / "nope")


def test_parity_supports_codex_harness(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = _seed_generated(tmp_path, {"demo": _demo_body("codex")})

    report = generated_parity_report(neutral, gen, harness="codex")

    assert report.harness == "codex"
    assert report.in_sync is True


# --- write_generated_skills (the regenerate / --fix path) -------------------


def test_write_generated_restores_parity(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = _seed_generated(tmp_path, {"demo": "drifted\n"})
    assert generated_parity_report(neutral, gen).in_sync is False

    written = write_generated_skills(neutral, gen)

    assert written == ["demo"]
    assert generated_parity_report(neutral, gen).in_sync is True


def test_write_generated_creates_missing(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC})
    gen = tmp_path / "generated"  # does not exist yet

    write_generated_skills(neutral, gen)

    assert (gen / "demo.md").read_text(encoding="utf-8") == _demo_body()


# --- real committed tree (the live gate) ------------------------------------


def test_real_shared_skills_are_in_sync_with_neutral() -> None:
    """Every committed shared-skills/<name>.md matches its neutral source.

    This is the assertion the CI parity gate enforces: the source-of-truth tree
    must never drift from the harness-neutral sources it is generated from.
    """
    report = generated_parity_report(_NEUTRAL_DIR, _SHARED_DIR)
    drifted = [f"{s.name}:{s.state.value}" for s in report.skills if s.state is not GeneratedState.IN_SYNC]
    assert report.in_sync is True, f"drift detected: {drifted}"


def test_real_neutral_covers_every_shared_skill() -> None:
    """The neutral sources cover exactly the seven shared skills.

    The body-mirror discovery deliberately excludes the pipeline skills
    (build-stories), which are full SKILL.md plugin skills checked separately.
    """
    discovered = set(discover_neutral_skills(_NEUTRAL_DIR))
    assert discovered == set(SHARED_SKILLS)


# --- pipeline-skill parity gate (Story 20.7-002) ----------------------------

# A minimal pipeline neutral source. `build-stories` is the only PIPELINE_SKILL,
# so the gate keys off that name.
_PIPE_NAME = "build-stories"
_PIPE_SRC = (
    "---\n"
    f"name: {_PIPE_NAME}\n"
    "description: Use when batch-building stories via the controller.\n"
    "allowed_tools:\n"
    "- Bash\n"
    "model_invocation: disabled\n"
    "---\n\n"
    "Run the controller.\n\n"
    "```bash\n"
    "sdlc build {{ARGUMENTS}}\n"
    "```\n"
)


def _seed_skill_base(root: Path, skills: dict[str, str]) -> Path:
    """Lay down a plugin skills base (`<name>/SKILL.md`) and return its path."""
    base = root / "skills"
    for name, text in skills.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _pipe_claude() -> str:
    return generate_claude_skill(parse_neutral_skill(_PIPE_SRC))


def test_build_stories_is_the_pipeline_skill() -> None:
    assert "build-stories" in PIPELINE_SKILLS


def test_discover_pipeline_sources_excludes_body_mirror(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {"demo": _DEMO_SRC, _PIPE_NAME: _PIPE_SRC})
    # The body-mirror discovery sees only the utility skill...
    assert set(discover_neutral_skills(neutral)) == {"demo"}
    # ...and the pipeline discovery sees only the pipeline skill.
    assert set(discover_pipeline_sources(neutral)) == {_PIPE_NAME}


def test_pipeline_parity_in_sync(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})

    report = pipeline_parity_report(neutral, base)

    assert isinstance(report, GeneratedParityReport)
    assert report.in_sync is True
    assert {s.name: s.state for s in report.skills} == {
        _PIPE_NAME: GeneratedState.IN_SYNC
    }


def test_pipeline_parity_detects_drift_with_diff(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: "hand-edited SKILL.md\n"})

    report = pipeline_parity_report(neutral, base)

    assert report.in_sync is False
    drifted = next(s for s in report.skills if s.name == _PIPE_NAME)
    assert drifted.state is GeneratedState.DRIFTED
    assert "regenerated" in drifted.diff
    assert "SKILL.md" in drifted.diff


def test_pipeline_parity_detects_missing(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {})  # no committed SKILL.md

    report = pipeline_parity_report(neutral, base)

    assert report.in_sync is False
    assert report.skills[0].state is GeneratedState.MISSING


def test_pipeline_parity_detects_orphan(tmp_path: Path) -> None:
    # A committed pipeline SKILL.md whose neutral source was deleted is an
    # orphan: it must surface so a stale generated file fails the gate.
    neutral = _seed_neutral(tmp_path, {})  # no pipeline source
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: "left behind\n"})

    report = pipeline_parity_report(neutral, base)

    assert report.in_sync is False
    assert report.skills[0].state is GeneratedState.ORPHAN
    assert report.skills[0].name == _PIPE_NAME


def test_pipeline_parity_unknown_harness_raises(tmp_path: Path) -> None:
    # An unknown harness has no full-SKILL.md generator, so rendering must fail
    # loudly rather than silently skip the parity check.
    neutral = _seed_neutral(tmp_path, {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})

    with pytest.raises(ValueError, match="unknown harness"):
        pipeline_parity_report(neutral, base, harness="bogus")


def test_pipeline_write_restores_parity(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path, {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: "drifted\n"})
    assert pipeline_parity_report(neutral, base).in_sync is False

    written = write_pipeline_skills(neutral, base)

    assert written == [_PIPE_NAME]
    assert pipeline_parity_report(neutral, base).in_sync is True


def test_pipeline_parity_missing_neutral_dir_raises(tmp_path: Path) -> None:
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})
    with pytest.raises(FileNotFoundError):
        pipeline_parity_report(tmp_path / "nope", base)


def test_real_build_stories_skill_is_in_sync_with_neutral() -> None:
    """The committed Claude build-stories SKILL.md matches its neutral source.

    This is the live pipeline parity gate: the plugin SKILL.md must never drift
    from the harness-neutral source it is generated from.
    """
    skill_base = _REPO_ROOT / "plugins" / "autonomous-sdlc" / "skills"
    report = pipeline_parity_report(_NEUTRAL_DIR, skill_base)
    drifted = [
        f"{s.name}:{s.state.value}"
        for s in report.skills
        if s.state is not GeneratedState.IN_SYNC
    ]
    assert report.in_sync is True, f"drift detected: {drifted}"
