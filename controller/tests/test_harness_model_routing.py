# ABOUTME: Tests for per-harness, per-stage model routing (Story 20.7-004) — the
# ABOUTME: OpenAI analog of Epic-14's Balanced map, so registry harnesses no longer ignore `model`.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.dispatch import resolve_agent_cmd
from sdlc.harness import (
    HarnessConfig,
    HarnessError,
    load_harnesses_config,
    resolve_harness,
)

# The repo's checked-in registry — the one a real run loads (proves AC2 against
# the real codex entry rather than a bespoke fixture).
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"


def _registry_harness(**overrides) -> HarnessConfig:
    base = dict(
        name="acme",
        command="acme run --model {model}",
        parser="codex-exec",
        models={"default": "acme-base", "build": "acme-pro", "merge": "acme-mini"},
        source="registry",
    )
    base.update(overrides)
    return HarnessConfig(**base)


# ---------------------------------------------------------------------------
# AC1: a registry entry with a {model} placeholder + a stage→model map
#      substitutes the mapped model for the routed stage.
# ---------------------------------------------------------------------------


def test_to_argv_substitutes_mapped_model_for_stage() -> None:
    cfg = _registry_harness()
    assert cfg.to_argv(stage="build") == ["acme", "run", "--model", "acme-pro"]
    assert cfg.to_argv(stage="merge") == ["acme", "run", "--model", "acme-mini"]


def test_registry_no_longer_ignores_model_routing() -> None:
    """Before 20.7-004 a registry entry rendered one fixed command for every stage."""
    cfg = _registry_harness()
    assert cfg.to_argv(stage="build") != cfg.to_argv(stage="merge")


def test_to_argv_appends_flags_after_substituted_model() -> None:
    """A {model} harness's invocation flags land *after* the substituted model.

    The `{model}` branch appends `flags` just like the static path; pin that the
    flags follow the routed model id rather than being dropped or reordered.
    """
    cfg = _registry_harness(flags=["--headless", "--json"])
    assert cfg.to_argv(stage="build") == [
        "acme", "run", "--model", "acme-pro", "--headless", "--json",
    ]
    # Unmapped stage still routes the default model, flags still trail it.
    assert cfg.to_argv(stage="coverage") == [
        "acme", "run", "--model", "acme-base", "--headless", "--json",
    ]


# ---------------------------------------------------------------------------
# AC3: a stage with no explicit mapping falls back to the harness `default`
#      model; a harness whose command has no {model} placeholder is unchanged.
# ---------------------------------------------------------------------------


def test_to_argv_falls_back_to_default_model_for_unmapped_stage() -> None:
    cfg = _registry_harness()
    # `coverage` is absent from the map → the harness `default` model.
    assert cfg.to_argv(stage="coverage") == ["acme", "run", "--model", "acme-base"]
    # No stage at all → also the default.
    assert cfg.to_argv() == ["acme", "run", "--model", "acme-base"]


def test_to_argv_static_command_unchanged_without_placeholder() -> None:
    """A harness that does not opt into {model} renders its static command (no regression)."""
    cfg = HarnessConfig(
        name="qwen", command="qwen-build-adapter.sh", parser="codex-exec",
        flags=["--headless"], source="registry",
    )
    assert cfg.to_argv(stage="build") == ["qwen-build-adapter.sh", "--headless"]
    assert cfg.to_argv() == ["qwen-build-adapter.sh", "--headless"]


def test_resolve_model_prefers_stage_then_default() -> None:
    cfg = _registry_harness()
    assert cfg.resolve_model("build") == "acme-pro"
    assert cfg.resolve_model("coverage") == "acme-base"  # falls back to default
    assert cfg.resolve_model(None) == "acme-base"


def test_resolve_model_none_without_models_map() -> None:
    cfg = HarnessConfig(name="q", command="q run", parser="codex-exec", source="registry")
    assert cfg.resolve_model("build") is None


# ---------------------------------------------------------------------------
# AC4: the Claude (builtin / env) slots keep Epic-14's routing untouched —
#      the routed `--model` alias decorates the Claude argv exactly as today.
# ---------------------------------------------------------------------------


def test_builtin_claude_model_routing_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)
    builtin = resolve_harness()
    assert builtin.source == "builtin"
    # The stage argument never touches the Claude slot; the alias still decorates.
    assert builtin.to_argv(model="opus", stage="build") == resolve_agent_cmd(model="opus")
    assert builtin.to_argv(model="opus", stage="build")[-2:] == ["--model", "opus"]


def test_env_slot_ignores_stage_and_model(monkeypatch) -> None:
    monkeypatch.setenv("SDLC_AGENT_CMD", "claude -p --model opus")
    override = resolve_harness()
    assert override.source == "env"
    # The escape hatch owns its own model; neither stage nor routed alias change it.
    assert override.to_argv(model="haiku", stage="merge") == ["claude", "-p", "--model", "opus"]


# ---------------------------------------------------------------------------
# AC2: the checked-in codex harness maps each pipeline stage to its own model.
# ---------------------------------------------------------------------------


def test_codex_harness_routes_each_stage_to_its_mapped_model() -> None:
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    assert "{model}" in codex.command
    # Each pipeline stage's worker launches with its own OpenAI model id.
    for stage in ("build", "coverage", "review", "merge", "adversarial"):
        argv = codex.to_argv(stage=stage)
        assert argv[0] == "codex-build-adapter.sh"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == codex.resolve_model(stage)
        # Still never spawns claude (AC of 20.3-001 preserved).
        assert not any("claude" in token for token in argv)


def test_codex_stage_models_differ_by_cost_tier() -> None:
    """The cheaper mechanical stages route to a different model than build/review."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    assert codex.resolve_model("merge") != codex.resolve_model("build")


def test_codex_unmapped_stage_uses_default_model() -> None:
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    # bugfix/reask are routable recovery stages absent from the AC stage list;
    # they fall back to the harness default rather than crashing.
    assert codex.resolve_model("bugfix") == codex.models["default"]


# ---------------------------------------------------------------------------
# Load + schema: a {model} command must declare a default; the map round-trips.
# ---------------------------------------------------------------------------


def test_load_models_map_round_trips(tmp_path: Path) -> None:
    cfg = tmp_path / "harnesses.yaml"
    cfg.write_text(
        "harnesses:\n"
        "  foo:\n"
        "    command: foo run --model {model}\n"
        "    parser: codex-exec\n"
        "    models:\n"
        "      default: foo-base\n"
        "      build: foo-pro\n",
        encoding="utf-8",
    )
    foo = load_harnesses_config(cfg)["foo"]
    assert foo.models == {"default": "foo-base", "build": "foo-pro"}


def test_load_rejects_model_placeholder_without_default(tmp_path: Path) -> None:
    """A `{model}` command with no `default` model can't resolve an unmapped stage."""
    bad = tmp_path / "harnesses.yaml"
    bad.write_text(
        "harnesses:\n"
        "  foo:\n"
        "    command: foo run --model {model}\n"
        "    parser: codex-exec\n"
        "    models:\n"
        "      build: foo-pro\n",
        encoding="utf-8",
    )
    with pytest.raises(HarnessError, match="default"):
        load_harnesses_config(bad)


def test_load_omitted_models_is_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "harnesses.yaml"
    cfg.write_text(
        "harnesses:\n  foo:\n    command: foo run\n    parser: codex-exec\n",
        encoding="utf-8",
    )
    assert load_harnesses_config(cfg)["foo"].models == {}


def test_schema_allows_models_property() -> None:
    from sdlc.harness import HARNESS_REGISTRY_SCHEMA

    harness_props = HARNESS_REGISTRY_SCHEMA["$defs"]["harness"]["properties"]
    assert "models" in harness_props


# ---------------------------------------------------------------------------
# AC2 end-to-end: a real codex-routed build loop launches each stage's worker
# with its mapped OpenAI model (not one fixed model for the whole run).
# ---------------------------------------------------------------------------


def test_build_loop_launches_each_codex_stage_with_its_mapped_model(tmp_path) -> None:
    from sdlc.build import BuildOptions, Ledger, run_build
    from sdlc.cohort import Story
    from sdlc.dispatch import AgentResult
    from sdlc.role_routing import PIPELINE_ROLES

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    payloads = {
        "build": {"branch_name": "feature/m1-001", "build_status": "SUCCESS", "commit_sha": "dead"},
        "coverage": {
            "pr_number": 7, "pr_url": "https://e/pull/7", "coverage_pct": 90.0,
            "tests_added": 1, "coverage_status": "PASS", "security_status": "PASS",
        },
        "review": {"pr_number": 7, "approval_status": "APPROVED", "change_count": 0, "final_status": "APPROVED"},
        "merge": {"pr_number": 7, "merge_status": "MERGED", "merge_sha": "cafe", "merged_at": "2026-06-27T00:00:00Z"},
    }

    seen_model: dict[str, str | None] = {}

    def dispatcher(agent_type, prompt, *, story=None, agent_cmd=None, parser=None, model=None, **kw):
        argv = agent_cmd or []
        seen_model[agent_type] = argv[argv.index("--model") + 1] if "--model" in argv else None
        return AgentResult(agent_type=agent_type, data=payloads[agent_type], raw="")

    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True,
        harness_map={role: "codex" for role in PIPELINE_ROLES},
    )
    run_build(
        opts,
        queue=[Story("m1-001", "Model story", "99", "sample", "epic-99.md", "P1", 2, "py", [])],
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )

    # Each stage launched with the model its `models` map assigns it.
    for stage in ("build", "coverage", "review", "merge"):
        assert seen_model[stage] == codex.resolve_model(stage)
    # And the cheaper mechanical stages really did route to a different model.
    assert seen_model["merge"] != seen_model["build"]
