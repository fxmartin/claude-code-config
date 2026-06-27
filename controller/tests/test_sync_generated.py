# ABOUTME: Tests for the cross-harness generated-skill parity gate (Story 20.4-003).
# ABOUTME: Verifies committed skill bodies match what their neutral source generates.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.skill_format import parse_neutral_skill, render_body
from sdlc.sync import (
    SHARED_SKILLS,
    GeneratedParityReport,
    GeneratedState,
    discover_neutral_skills,
    generated_parity_report,
    write_generated_skills,
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
    """The neutral sources cover exactly the seven shared skills."""
    discovered = set(discover_neutral_skills(_NEUTRAL_DIR))
    assert discovered == set(SHARED_SKILLS)
