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


# --- pipeline-skill parity gate (Story 20.7-002) ----------------------------

from sdlc.skill_generator import generate_claude_skill  # noqa: E402

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
    base = root / "skills"
    for name, text in skills.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _pipe_claude() -> str:
    return generate_claude_skill(parse_neutral_skill(_PIPE_SRC))


def test_sync_check_skill_base_in_sync_exits_zero(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})
    # An empty body-mirror source dir keeps that check trivially in sync.
    src = _seed(tmp_path / "src", {})

    result = runner.invoke(
        app,
        ["sync-check", str(src), "--neutral", str(neutral), "--skill-base", str(base)],
    )

    assert result.exit_code == 0
    assert "pipeline skills in sync" in result.output.lower()


def test_sync_check_skill_base_drift_exits_one_with_fix_hint(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: "hand-edited drift\n"})
    src = _seed(tmp_path / "src", {})

    result = runner.invoke(
        app,
        ["sync-check", str(src), "--neutral", str(neutral), "--skill-base", str(base)],
    )

    assert result.exit_code == 1
    assert _PIPE_NAME in result.output
    assert "--fix" in result.output


def test_sync_check_skill_base_fix_rewrites_and_exits_zero(tmp_path: Path) -> None:
    neutral = _seed_neutral(tmp_path / "neutral", {_PIPE_NAME: _PIPE_SRC})
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: "drifted\n"})
    src = _seed(tmp_path / "src", {})

    result = runner.invoke(
        app,
        [
            "sync-check",
            str(src),
            "--neutral",
            str(neutral),
            "--skill-base",
            str(base),
            "--fix",
        ],
    )

    assert result.exit_code == 0
    assert (base / _PIPE_NAME / "SKILL.md").read_text(
        encoding="utf-8"
    ) == _pipe_claude()


def test_sync_check_skill_base_without_neutral_exits_two(tmp_path: Path) -> None:
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})
    src = _seed(tmp_path / "src", {})

    result = runner.invoke(app, ["sync-check", str(src), "--skill-base", str(base)])

    assert result.exit_code == 2
    assert "--skill-base requires --neutral" in result.output


def test_sync_check_skill_base_malformed_source_exits_two(tmp_path: Path) -> None:
    # A malformed pipeline source slips past the body-mirror gate (which excludes
    # pipeline skills) but fails the pipeline gate when it tries to render it, so
    # the --skill-base error handler must turn that into a clean exit 2.
    neutral = _seed_neutral(
        tmp_path / "neutral", {_PIPE_NAME: "not valid frontmatter\n"}
    )
    base = _seed_skill_base(tmp_path, {_PIPE_NAME: _pipe_claude()})
    src = _seed(tmp_path / "src", {})

    result = runner.invoke(
        app,
        ["sync-check", str(src), "--neutral", str(neutral), "--skill-base", str(base)],
    )

    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_sync_check_fix_without_neutral_exits_two(tmp_path: Path) -> None:
    # --fix only makes sense for the generated-parity gate, so it requires
    # --neutral; asking for it against a consumer mirror is a usage error.
    src = _seed(tmp_path / "src", {"roast": "x"})
    consumer = _seed(tmp_path / "consumer", {"roast": "x"})

    result = runner.invoke(
        app, ["sync-check", str(src), str(consumer), "--fix"]
    )

    assert result.exit_code == 2
    assert "--fix requires --neutral" in result.output
