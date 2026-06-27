# ABOUTME: Behavior tests for the `sdlc sync-check` subcommand (Story 7.4-001).
# ABOUTME: Filesystem-only; verifies the parity verdict surfaces through the CLI.

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


def _seed(dir_path: Path, skills: dict[str, str]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, body in skills.items():
        (dir_path / f"{name}.md").write_text(body, encoding="utf-8")
    return dir_path


def test_sync_check_in_sync_exits_zero(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "x", "coverage": "y"})
    consumer = _seed(tmp_path / "consumer", {"roast": "x", "coverage": "y"})

    result = runner.invoke(app, ["sync-check", str(src), str(consumer)])

    assert result.exit_code == 0
    assert "in sync" in result.output.lower()


def test_sync_check_drift_exits_nonzero_and_names_skill(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "new"})
    consumer = _seed(tmp_path / "consumer", {"roast": "old"})

    result = runner.invoke(app, ["sync-check", str(src), str(consumer)])

    assert result.exit_code == 1
    assert "roast" in result.output
    assert "drift" in result.output.lower()


def test_sync_check_missing_dir_exits_two(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "x"})

    result = runner.invoke(app, ["sync-check", str(src), str(tmp_path / "nope")])

    assert result.exit_code == 2
    assert "error" in result.output.lower()


# --- generated-skill parity gate (Story 20.4-003) ---------------------------

from sdlc.skill_format import parse_neutral_skill, render_body  # noqa: E402

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


def _seed_neutral(dir_path: Path, sources: dict[str, str]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        (dir_path / f"{name}.skill.md").write_text(text, encoding="utf-8")
    return dir_path


def test_sync_check_generated_in_sync_exits_zero(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {"demo": _DEMO_SRC})
    gen = _seed(tmp_path / "gen", {"demo": _demo_body()})

    result = runner.invoke(app, ["sync-check", str(gen), "--neutral", str(neutral)])

    assert result.exit_code == 0
    assert "in sync" in result.output.lower()


def test_sync_check_generated_drift_exits_one_with_diff(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {"demo": _DEMO_SRC})
    gen = _seed(tmp_path / "gen", {"demo": "hand-edited drift\n"})

    result = runner.invoke(app, ["sync-check", str(gen), "--neutral", str(neutral)])

    assert result.exit_code == 1
    assert "demo" in result.output
    # The failure names a concrete regenerate command (AC: "diff and the
    # regenerate command").
    assert "--fix" in result.output


def test_sync_check_generated_missing_neutral_dir_exits_two(tmp_path: Path) -> None:
    gen = _seed(tmp_path / "gen", {"demo": _demo_body()})

    result = runner.invoke(
        app, ["sync-check", str(gen), "--neutral", str(tmp_path / "nope")]
    )

    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_sync_check_fix_rewrites_drift_and_exits_zero(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {"demo": _DEMO_SRC})
    gen = _seed(tmp_path / "gen", {"demo": "drifted\n"})

    result = runner.invoke(
        app, ["sync-check", str(gen), "--neutral", str(neutral), "--fix"]
    )

    assert result.exit_code == 0
    assert (gen / "demo.md").read_text(encoding="utf-8") == _demo_body()


def test_sync_check_no_consumer_no_neutral_exits_two(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "x"})

    result = runner.invoke(app, ["sync-check", str(src)])

    assert result.exit_code == 2
    assert "error" in result.output.lower()
