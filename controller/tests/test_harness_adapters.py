# ABOUTME: Tests for the "add a new harness" guide + generic CLI adapter template (Story 20.6-001).
# ABOUTME: Proves the worked-example registry is schema-valid and the template round-trips the contract.

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from sdlc.capability import CAPABILITY_KEYS
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


def test_adapter_fail_fast_uses_exit_64_and_names_itself() -> None:
    """The guide promises 'fails fast (exit 64)' with a message naming the knob.

    Existing coverage only asserts a non-zero exit; pin the exact EX_USAGE code
    and the actionable first line so the documented contract cannot silently
    drift to a different exit status or a vague message.
    """
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="anything",
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": _system_path()},
    )
    assert proc.returncode == 64
    # The first stderr line names the script so the operator knows what failed.
    assert proc.stderr.splitlines()[0].startswith("generic-cli-adapter")
    # And the guide advertises exactly this exit code.
    assert "exit 64" in GUIDE.read_text(encoding="utf-8").lower()


def test_adapter_self_test_runs_without_agent_cmd_configured() -> None:
    """`--self-test` short-circuits before the AGENT_CMD check (no CLI needed)."""
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE), "--self-test"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": _system_path()},  # deliberately no HARNESS_AGENT_CMD
    )
    assert proc.returncode == 0, proc.stderr
    assert RESULT_START_MARKER in proc.stdout


def test_adapter_delivers_prompt_on_agent_stdin() -> None:
    """Contract point #1: the controller's prompt reaches the agent on its stdin.

    The stand-in CLI branches on what it reads from stdin, so a SUCCESS block can
    only appear if the wrapper actually piped the prompt through.
    """
    fake_cli = (
        'read -r line; '
        'if [ "$line" = "ping" ]; then status=SUCCESS; else status=FAILED; fi; '
        'printf \'%s\\n{"branch_name": "feature/9.9-999", "build_status": "%s", '
        '"commit_sha": "deadbee"}\\n%s\\n\' '
        '"$START" "$status" "$END"'
    )
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="ping",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HARNESS_AGENT_CMD": fake_cli,
            "START": RESULT_START_MARKER,
            "END": RESULT_END_MARKER,
            "PATH": _system_path(),
        },
    )
    assert proc.returncode == 0, proc.stderr
    data = parse_and_validate("build", proc.stdout)
    assert data["build_status"] == "SUCCESS"  # only true if "ping" arrived on stdin


def test_adapter_forwards_surrounding_prose_verbatim() -> None:
    """The wrapper forwards CLI stdout verbatim — prose around the block survives."""
    block = (
        f"{RESULT_START_MARKER}\n"
        '{"branch_name": "feature/9.9-999", '
        '"build_status": "SUCCESS", "commit_sha": "abc1234"}\n'
        f"{RESULT_END_MARKER}\n"
    )
    fake_cli = 'cat >/dev/null; printf "%s" "$PROSE_BLOCK"'
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="build please",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HARNESS_AGENT_CMD": fake_cli,
            "PROSE_BLOCK": f"thinking out loud...\n{block}trailing note\n",
            "PATH": _system_path(),
        },
    )
    assert proc.returncode == 0, proc.stderr
    # Prose on both sides of the block round-trips untouched.
    assert "thinking out loud..." in proc.stdout
    assert "trailing note" in proc.stdout
    # And the contract parser still recovers the block from the surrounding prose.
    assert parse_and_validate("build", proc.stdout)["commit_sha"] == "abc1234"


def test_adapter_propagates_agent_nonzero_exit() -> None:
    """Contract point #4: a non-zero exit from the underlying CLI is a failure.

    `exec` replaces the wrapper, so the agent CLI's exit status is the wrapper's.
    """
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE)],
        input="anything",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HARNESS_AGENT_CMD": "cat >/dev/null; exit 7",
            "PATH": _system_path(),
        },
    )
    assert proc.returncode == 7


def test_adapter_matches_self_test_flag_exactly() -> None:
    """An unrelated first arg is NOT treated as `--self-test`; it falls through.

    Without a configured CLI that means the actionable fail-fast path, proving
    the self-test branch is matched exactly rather than for any argument.
    """
    proc = subprocess.run(
        ["bash", str(ADAPTER_TEMPLATE), "--not-self-test"],
        input="anything",
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": _system_path()},
    )
    assert proc.returncode == 64
    assert RESULT_START_MARKER not in proc.stdout
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


def test_guide_documents_self_test_round_trip_proof() -> None:
    """The guide tells operators to prove the contract with `--self-test`."""
    text = GUIDE.read_text(encoding="utf-8")
    assert "--self-test" in text
    # The proof is anchored to the real template path operators copy from.
    assert "generic-cli-adapter.sh --self-test" in text


def test_guide_recommends_reusing_codex_exec_parser() -> None:
    """The 'no new parser' guidance names codex-exec, and it must be registered."""
    text = GUIDE.read_text(encoding="utf-8")
    assert "codex-exec" in text
    assert "codex-exec" in set(parser_ids())
    # Every worked-example block reuses that recommended id (no bespoke parser).
    for block in _registry_blocks():
        for entry in block["harnesses"].values():
            assert entry["parser"] == "codex-exec"


def test_guide_example_capabilities_use_only_canonical_flags() -> None:
    """Worked-example capability flags match the documented canonical set.

    A typo'd flag silently resolves to false (an undeclared capability is assumed
    absent), so it would degrade the harness without erroring. Pin the example's
    flags to the real ``CAPABILITY_KEYS`` so a guide typo is caught here.
    """
    canonical = set(CAPABILITY_KEYS)
    checked = False
    for block in _registry_blocks():
        for entry in block["harnesses"].values():
            declared = set(entry.get("capabilities", {}))
            if not declared:
                continue
            checked = True
            assert declared <= canonical, (
                f"unknown capability flag(s) {declared - canonical}; "
                f"canonical set is {sorted(canonical)}"
            )
    assert checked, "guide must show at least one capabilities: block"


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
