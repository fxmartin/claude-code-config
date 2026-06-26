# ABOUTME: Tests for the config-driven harness registry and adapter contract (Story 20.1-001).
# ABOUTME: Covers schema contract, config load/validate, default fallback, and env-override path.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.dispatch import DEFAULT_AGENT_CMD, resolve_agent_cmd
from sdlc.harness import (
    DEFAULT_HARNESS,
    HARNESS_REGISTRY_SCHEMA,
    HarnessConfig,
    HarnessError,
    load_harnesses_config,
    resolve_agent_argv,
    resolve_harness,
)

# The repo's checked-in default registry.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_schema_declares_draft_2020_12() -> None:
    assert HARNESS_REGISTRY_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


# ---------------------------------------------------------------------------
# Config load + validate (AC1)
# ---------------------------------------------------------------------------


def test_checked_in_config_loads() -> None:
    registry = load_harnesses_config(CONFIG_PATH)
    assert DEFAULT_HARNESS in registry
    assert "codex" in registry


def test_loaded_entry_resolves_all_four_aspects() -> None:
    """AC1: each entry resolves a command template, flags, capabilities, parser id."""
    registry = load_harnesses_config(CONFIG_PATH)
    claude = registry[DEFAULT_HARNESS]
    assert claude.command.startswith("claude -p")
    assert claude.parser == "claude-stream-json"
    assert claude.capabilities["worktree_isolation"] is True
    assert isinstance(claude.flags, list)
    assert claude.enabled is True


def test_codex_capabilities_loaded_from_yaml() -> None:
    registry = load_harnesses_config(CONFIG_PATH)
    codex = registry["codex"]
    assert codex.parser == "codex-exec"
    assert codex.capabilities["usage_tracking"] is False
    assert codex.capabilities["json_contract"] is True


def test_load_rejects_non_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "harnesses.yaml"
    bad.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(HarnessError):
        load_harnesses_config(bad)


def test_load_rejects_entry_missing_command(tmp_path: Path) -> None:
    bad = tmp_path / "harnesses.yaml"
    bad.write_text("harnesses:\n  foo:\n    parser: plain\n", encoding="utf-8")
    with pytest.raises(HarnessError):
        load_harnesses_config(bad)


def test_load_rejects_entry_missing_parser(tmp_path: Path) -> None:
    bad = tmp_path / "harnesses.yaml"
    bad.write_text("harnesses:\n  foo:\n    command: foo run\n", encoding="utf-8")
    with pytest.raises(HarnessError):
        load_harnesses_config(bad)


def test_load_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    bad = tmp_path / "harnesses.yaml"
    bad.write_text(
        "harnesses:\n  foo:\n    command: foo\n    parser: plain\nbogus: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(HarnessError):
        load_harnesses_config(bad)


def test_load_rejects_empty_harnesses(tmp_path: Path) -> None:
    bad = tmp_path / "harnesses.yaml"
    bad.write_text("harnesses: {}\n", encoding="utf-8")
    with pytest.raises(HarnessError):
        load_harnesses_config(bad)


def test_load_applies_enabled_default(tmp_path: Path) -> None:
    cfg = tmp_path / "harnesses.yaml"
    cfg.write_text("harnesses:\n  foo:\n    command: foo run\n    parser: plain\n", encoding="utf-8")
    registry = load_harnesses_config(cfg)
    assert registry["foo"].enabled is True


# ---------------------------------------------------------------------------
# Command rendering + placeholder templating
# ---------------------------------------------------------------------------


def test_render_command_appends_flags() -> None:
    cfg = HarnessConfig(
        name="foo", command="foo run", parser="plain", flags=["--json"],
    )
    assert cfg.render_command() == ["foo", "run", "--json"]


def test_render_command_substitutes_placeholders() -> None:
    cfg = HarnessConfig(
        name="foo", command="foo review --pr {pr_number} --story {story_id}", parser="plain",
    )
    argv = cfg.render_command(pr_number=42, story_id="20.1-001")
    assert argv == ["foo", "review", "--pr", "42", "--story", "20.1-001"]


# ---------------------------------------------------------------------------
# Default fallback (AC2): no registry, no env override -> byte-identical default
# ---------------------------------------------------------------------------


def test_resolve_default_is_builtin_when_no_config(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)
    harness = resolve_harness()
    assert harness.name == DEFAULT_HARNESS
    assert harness.source == "builtin"


def test_resolve_default_argv_byte_identical_to_dispatch(monkeypatch) -> None:
    """AC2: argv with no registry/env is byte-identical to today's DEFAULT_AGENT_CMD path."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)
    argv = resolve_agent_argv()
    assert argv == resolve_agent_cmd()
    assert argv[: len(DEFAULT_AGENT_CMD)] == DEFAULT_AGENT_CMD


def test_resolve_default_argv_carries_routed_model(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)
    argv = resolve_agent_argv(model="sonnet")
    assert argv == resolve_agent_cmd(model="sonnet")
    assert argv[-2:] == ["--model", "sonnet"]


def test_resolve_builtin_capabilities_all_true(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    harness = resolve_harness()
    assert all(harness.capabilities.values())
    assert harness.capabilities["worktree_isolation"] is True


# ---------------------------------------------------------------------------
# Env override (AC3): SDLC_AGENT_CMD re-expressed as an ad-hoc registry entry
# ---------------------------------------------------------------------------


def test_resolve_env_override_is_adhoc_entry(monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --permission-mode acceptEdits")
    harness = resolve_harness()
    assert harness.source == "env"
    assert harness.command == "claude -p --permission-mode acceptEdits"


def test_resolve_env_override_argv_matches_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --permission-mode acceptEdits")
    argv = resolve_agent_argv()
    assert argv == resolve_agent_cmd()
    assert argv == ["claude", "-p", "--permission-mode", "acceptEdits"]


def test_resolve_env_override_ignores_routed_model(monkeypatch) -> None:
    """AC3: the escape hatch owns its model; the routed model never decorates it."""
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --model opus")
    argv = resolve_agent_argv(model="haiku")
    assert argv == ["claude", "-p", "--model", "opus"]


# ---------------------------------------------------------------------------
# Named harness resolution from the registry
# ---------------------------------------------------------------------------


def test_resolve_named_harness_from_registry(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    harness = resolve_harness("codex", config_path=CONFIG_PATH)
    assert harness.source == "registry"
    assert harness.name == "codex"
    assert resolve_agent_argv("codex", config_path=CONFIG_PATH) == ["codex", "exec"]


def test_resolve_unknown_harness_raises(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    with pytest.raises(HarnessError):
        resolve_harness("nope", config_path=CONFIG_PATH)


def test_resolve_named_harness_requires_config() -> None:
    """A non-default named harness with no registry to resolve it is an error."""
    with pytest.raises(HarnessError):
        resolve_harness("codex")
