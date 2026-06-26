# ABOUTME: Tests for the "add a new harness" guide + generic CLI adapter template (Story 20.6-001).
# ABOUTME: Proves the worked-example registry is schema-valid and the template round-trips the contract.

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from sdlc.contracts import (
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    parse_and_validate,
)
from sdlc.harness import HARNESS_REGISTRY_SCHEMA, load_harnesses_config
from sdlc.parsers import parser_ids

# Layout: this file is controller/tests/test_harness_adapters.py.
_CONTROLLER = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CONTROLLER.parent
ADAPTER_TEMPLATE = _CONTROLLER / "adapters" / "generic-cli-adapter.sh"
GUIDE = _REPO_ROOT / "docs" / "harness-adapters.md"
ROOT_README = _REPO_ROOT / "README.md"

# A ```yaml fenced block in the guide.
_YAML_FENCE_RE = re.compile(r"```ya?ml\n(.*?)```", re.DOTALL)


def _registry_blocks() -> list[dict]:
    """Every fenced YAML block in the guide that declares a harness registry."""
    text = GUIDE.read_text(encoding="utf-8")
    blocks: list[dict] = []
    for body in _YAML_FENCE_RE.findall(text):
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and "harnesses" in parsed:
            blocks.append(parsed)
    return blocks


# ---------------------------------------------------------------------------
# The generic adapter template (AC2: round-trips the contract out of the box)
# ---------------------------------------------------------------------------


def test_adapter_template_exists_and_is_bash() -> None:
    assert ADAPTER_TEMPLATE.is_file()
    first_line = ADAPTER_TEMPLATE.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "bash" in first_line


def test_adapter_self_test_emits_valid_contract_block() -> None:
    """`--self-test` round-trips a schema-valid <<<RESULT_JSON>>> block (AC2)."""
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE), "--self-test"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert RESULT_START_MARKER in proc.stdout
    assert RESULT_END_MARKER in proc.stdout
    # The block must satisfy the real build-agent contract, unedited.
    data = parse_and_validate("build", proc.stdout)
    assert data["build_status"] in ("SUCCESS", "FAILED")


def test_adapter_passes_through_upstream_result_block() -> None:
    """The wrapper forwards an underlying agent's contract block unchanged."""
    block = (
        f"{RESULT_START_MARKER}\n"
        '{"branch_name": "feature/9.9-999", '
        '"build_status": "SUCCESS", '
        '"commit_sha": "abc1234"}\n'
        f"{RESULT_END_MARKER}\n"
    )
    # A stand-in agent CLI: drains the prompt on stdin, emits the contract block.
    # The block is carried in an env var so its real newlines survive intact.
    fake_cli = 'cat >/dev/null; printf "%s" "$RESULT_BLOCK"'
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="build story 9.9-999 please",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HARNESS_AGENT_CMD": fake_cli,
            "RESULT_BLOCK": block,
            "PATH": _system_path(),
        },
    )
    assert proc.returncode == 0, proc.stderr
    data = parse_and_validate("build", proc.stdout)
    assert data["branch_name"] == "feature/9.9-999"
    assert data["commit_sha"] == "abc1234"


def test_adapter_fails_fast_without_agent_cmd() -> None:
    """Unconfigured and not self-testing → actionable non-zero exit, no silent run."""
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="anything",
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": _system_path()},
    )
    assert proc.returncode != 0
    assert "HARNESS_AGENT_CMD" in proc.stderr


def _system_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# ---------------------------------------------------------------------------
# The guide (AC1: worked example with no Python edits)
# ---------------------------------------------------------------------------


def test_guide_exists() -> None:
    assert GUIDE.is_file()


def test_guide_covers_the_four_moving_parts() -> None:
    text = GUIDE.read_text(encoding="utf-8").lower()
    # harnesses.yaml entry, wrapper template, parser declaration, capability flags.
    assert "harnesses.yaml" in text
    assert "generic-cli-adapter.sh" in text
    assert "parser" in text
    assert "capabilities" in text
    # The contract is delivered on stdin and returned as the result block.
    assert "stdin" in text
    assert RESULT_START_MARKER.lower() in text
    # No Python edits required — the whole point of the abstraction.
    assert "no python" in text


def test_guide_references_codex_and_future_targets() -> None:
    text = GUIDE.read_text(encoding="utf-8").lower()
    assert "codex" in text  # canonical worked example
    for target in ("opencode", "pi", "gemini"):
        assert target in text


def test_guide_registry_blocks_are_schema_valid() -> None:
    """Every worked-example harnesses.yaml block validates against the schema (AC1)."""
    blocks = _registry_blocks()
    assert blocks, "guide must contain at least one harnesses.yaml worked example"
    validator = Draft202012Validator(HARNESS_REGISTRY_SCHEMA)
    for block in blocks:
        errors = sorted(validator.iter_errors(block), key=lambda e: list(e.absolute_path))
        assert not errors, f"invalid registry block: {[e.message for e in errors]}"


def test_guide_example_parsers_are_registered() -> None:
    """Parser ids used in the worked example must already exist — no Python edits."""
    registered = set(parser_ids())
    for block in _registry_blocks():
        for entry in block["harnesses"].values():
            assert entry["parser"] in registered, (
                f"parser {entry['parser']!r} is not registered; "
                f"the guide must reuse an existing parser id ({sorted(registered)})"
            )


def test_guide_example_round_trips_through_loader(tmp_path: Path) -> None:
    """A worked-example block loads cleanly through the real registry loader."""
    blocks = _registry_blocks()
    loaded_any = False
    for block in blocks:
        cfg = tmp_path / "harnesses.yaml"
        cfg.write_text(yaml.safe_dump(block), encoding="utf-8")
        registry = load_harnesses_config(cfg)
        assert registry
        loaded_any = True
    assert loaded_any


# ---------------------------------------------------------------------------
# Discoverability (DoD: linked from the README harness matrix)
# ---------------------------------------------------------------------------


def test_readme_links_to_the_guide() -> None:
    assert "docs/harness-adapters.md" in ROOT_README.read_text(encoding="utf-8")
