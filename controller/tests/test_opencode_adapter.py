# ABOUTME: Tests for the OpenCode harness adapter registry integration (Story 29.2-001).
# ABOUTME: Proves OpenCode dispatch uses the opencode-json parser and never invokes Claude.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdlc.capability import MODE_PARALLEL, preflight_harness, resolve_capabilities
from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.degradation import DegradationKind, evaluate_degradations
from sdlc.dispatch import AgentDispatchError
from sdlc.harness import dispatch_on_harness, load_harnesses_config, resolve_harness
from sdlc.parsers import OPENCODE_PARSER_ID, OpenCodeJsonParser, get_parser
from sdlc.role_routing import PIPELINE_ROLES, resolve_role_routing

CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"

_VALID_BUILD = {
    "branch_name": "feature/opencode",
    "build_status": "SUCCESS",
    "commit_sha": "feedface",
}

_VALID_COVERAGE = {
    "pr_number": 42,
    "pr_url": "https://github.com/fxmartin/repo/pull/42",
    "coverage_pct": 91.5,
    "tests_added": 7,
    "coverage_status": "PASS",
    "security_status": "PASS",
}


def _wrap(payload: dict) -> str:
    body = json.dumps(payload)
    return f"opencode prose and reasoning\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_opencode_entry_uses_opencode_json_parser() -> None:
    # Story 29.2-003: switched off the no-telemetry plain parser onto the
    # `--format json` event-stream parser that recovers real usage/cost.
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    assert opencode.parser == OPENCODE_PARSER_ID
    assert isinstance(get_parser(opencode.parser), OpenCodeJsonParser)


def test_opencode_entry_declares_probe_and_capabilities() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    assert opencode.probe == "opencode --version"
    assert opencode.capabilities["json_contract"] is True
    # worktree_isolation / parallel verified true by Story 29.2-002 (evidence:
    # controller/eval/results/opencode-worktree-smoke-29.2-002.json) — see the
    # degradation tests below for the preflight consequence.
    assert opencode.capabilities["worktree_isolation"] is True
    assert opencode.capabilities["parallel"] is True
    # Story 29.2-003: flipped true once the `opencode-json` parser could
    # actually recover real per-session tokens/cost off `step_finish` events.
    assert opencode.capabilities["usage_tracking"] is True
    assert opencode.capabilities["rate_limit_aware"] is False


def test_opencode_argv_never_invokes_claude() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    argv = opencode.to_argv()
    assert argv[0] == "opencode-build-adapter.sh"
    # The executable must be the opencode wrapper, never the claude CLI. A
    # pinned `anthropic/claude-*` *model* id is a different thing entirely — it
    # is the Anthropic API reached through OpenCode, not Claude Code spawned as
    # a harness — so the guard checks the command, not every token.
    assert "claude" not in argv[0]
    assert not any(tok == "claude" or tok.endswith("/claude") for tok in argv)


def test_shipped_opencode_harness_pins_a_reachable_model_every_stage() -> None:
    """The entry pins a model deliberately, inverting the issue-#228 instinct.

    #228 says never hardcode a model id, because an unentitled pin fails. That
    holds for `codex exec`, which errors fast on a model you cannot reach. It
    does NOT hold for OpenCode: with no `--model` it resolves whatever its own
    config names as default, and an unreachable provider hangs instead of
    failing. The captured dispatch path has no stall detector
    (``dispatch.py:713``), so ``DEFAULT_TIMEOUT_S`` (3600s) is the only
    backstop — one silent hour per stage.

    Between "an unentitled pin fails loudly" and "an unreachable default hangs
    for an hour", the pin is the safer default. `OPENCODE_FLAGS='--model <id>'`
    remains the redeploy-safe override for anyone entitled differently.
    """
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    for stage in (None, "build", "coverage", "review", "merge", "adversarial"):
        argv = opencode.to_argv() if stage is None else opencode.to_argv(stage=stage)
        assert "--model" in argv, (
            f"shipped opencode argv resolves no model for stage={stage}: {argv}"
        )
        model = argv[argv.index("--model") + 1]
        assert "/" in model, f"stage={stage} model is not a provider/model id: {model}"
        assert "{model}" not in model, f"stage={stage} left the placeholder unresolved"


def test_opencode_entry_documents_the_unreachable_default_hang() -> None:
    """The entry pins no model, so the *documentation* of that trade-off is the
    only mitigation a user gets before burning a silent 3600s stage. Pin it: an
    edit that drops the warning or the escape hatch must fail here."""
    entry = CONFIG_PATH.read_text().split("  opencode:")[0].rsplit("# OpenCode adapter", 1)[-1]
    assert "OPENCODE_FLAGS" in entry, (
        "opencode entry no longer names the redeploy-safe model override"
    )
    assert "opencode run --pure" in entry, (
        "opencode entry no longer shows how to check the resolved default answers"
    )
    assert "3600" in entry, (
        "opencode entry no longer states the wall clock a silent hang costs"
    )


def _wrap_ndjson(payload: dict, *, session_id: str = "ses_test1") -> str:
    """A realistic single-step `--format json` NDJSON stream carrying ``payload``.

    Mirrors the real shape verified against OpenCode 1.18.15 (Story 29.2-003):
    a ``text`` event's ``part.text`` carries the contract block, a
    ``step_finish`` event's ``part.tokens``/``part.cost`` carry real usage.
    """
    text_event = json.dumps(
        {"type": "text", "sessionID": session_id, "part": {"type": "text", "text": _wrap(payload)}}
    )
    finish_event = json.dumps(
        {
            "type": "step_finish",
            "sessionID": session_id,
            "part": {
                "type": "step-finish",
                "tokens": {
                    "total": 118,
                    "input": 3,
                    "output": 5,
                    "reasoning": 0,
                    "cache": {"write": 100, "read": 10},
                },
                "cost": 0.02,
            },
        }
    )
    return f"{text_event}\n{finish_event}\n"


def test_build_agent_round_trips_through_opencode(monkeypatch) -> None:
    seen_cmd: list[str] = []
    seen_input: list[str | None] = []

    def fake_run(cmd, **kwargs):
        seen_cmd[:] = cmd
        seen_input.append(kwargs.get("input"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    result = dispatch_on_harness(opencode, "build", "build story with opencode")

    assert result.data["build_status"] == "SUCCESS"
    assert result.data["commit_sha"] == "feedface"
    # Story 29.2-003: usage_tracking is now a real harness-level capability, so
    # usage_available is True even on this plain-text fixture (no NDJSON
    # events at all) — the run itself simply carried no usable telemetry, so
    # usage/cost stay None rather than a fabricated zero (see the NDJSON test
    # below for the "real usage recorded" path).
    assert result.usage_available is True
    assert result.usage is None
    assert result.cost_usd is None
    # The dispatched command is the opencode wrapper, not the claude CLI; the
    # pinned anthropic/claude-* model id rides in argv as data, not as an exe.
    assert "claude" not in seen_cmd[0]
    assert not any(tok == "claude" or tok.endswith("/claude") for tok in seen_cmd)
    assert seen_input == ["build story with opencode"]


def test_build_agent_round_trip_through_opencode_records_real_usage(monkeypatch) -> None:
    """Story 29.2-003 AC1/DoD: a real `--format json` stream's `step_finish`
    tokens/cost land on the AgentResult the ledger's `stage_set_usage` reads —
    the "dashboard/ledger show real usage on an opencode-routed stage" bar."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap_ndjson(_VALID_BUILD)))

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    result = dispatch_on_harness(opencode, "build", "build story with opencode")

    assert result.data["build_status"] == "SUCCESS"
    assert result.usage_available is True
    assert result.usage == {
        "input_tokens": 3,
        "output_tokens": 5,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 100,
    }
    assert result.cost_usd == pytest.approx(0.02)
    assert result.session_id == "ses_test1"


def test_coverage_agent_round_trips_through_opencode(monkeypatch) -> None:
    """Only the build role is proven live per the story's field finding — this
    proves the coverage role at least round-trips through the same wrapper and
    parser, so "any pipeline role" is not resting solely on the build self-test."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_COVERAGE))
    )

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    result = dispatch_on_harness(opencode, "coverage", "qa story with opencode")

    assert result.data["coverage_status"] == "PASS"
    assert result.usage_available is True
    assert result.usage is None


def test_opencode_nonzero_exit_is_plain_dispatch_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="opencode blew up"),
    )

    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    with pytest.raises(AgentDispatchError) as excinfo:
        dispatch_on_harness(opencode, "build", "prompt")
    assert type(excinfo.value) is AgentDispatchError


def test_full_opencode_run_spawns_zero_claude() -> None:
    role_map = {role: "opencode" for role in PIPELINE_ROLES}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)

    assert set(resolved) == set(PIPELINE_ROLES)
    for role, harness in resolved.items():
        assert harness.name == "opencode", f"role {role} did not route to opencode"
        argv = harness.to_argv()
        # No claude *binary* on any role's argv; a pinned anthropic/claude-*
        # model id is the API behind OpenCode, not the Claude Code harness.
        assert "claude" not in argv[0]
        assert not any(tok == "claude" or tok.endswith("/claude") for tok in argv)


def test_registry_bare_adapter_commands_are_installed_on_path() -> None:
    """Regression guard for Story 29.2-001's dispatch gap.

    Registry entries invoke their wrapper by BARE NAME, resolved on PATH at
    dispatch, and `install.sh --core` is what puts those wrappers on PATH. A new
    harness whose adapter the installer does not symlink resolves to nothing on a
    PATH-installed controller and the stage dies with "command not found" — which
    is exactly what the shipped `opencode` entry did. Assert the invariant for
    every registry adapter, not just this story's.
    """
    core_sh = Path(__file__).resolve().parents[2] / "install" / "core.sh"
    if not core_sh.is_file():
        pytest.skip("install/core.sh is not present (installed controller, not a checkout)")
    installer = core_sh.read_text(encoding="utf-8")

    registry = load_harnesses_config(CONFIG_PATH)
    adapters = sorted(
        {
            token
            for entry in registry.values()
            for token in [entry.command.split()[0]]
            if token.endswith("-adapter.sh") and "/" not in token
        }
    )
    assert "opencode-build-adapter.sh" in adapters
    for adapter in adapters:
        assert f'create_symlink "$SCRIPT_DIR/scripts/{adapter}"' in installer, (
            f"{adapter} is dispatched by bare name but install/core.sh never "
            f"symlinks it onto PATH"
        )
        assert f'remove_symlink "$bin_dir/{adapter}"' in installer, (
            f"{adapter} is symlinked onto PATH but --uninstall never removes it"
        )


# ---------------------------------------------------------------------------
# Story 29.2-002: worktree_isolation/parallel verified true (evidence:
# controller/eval/results/opencode-worktree-smoke-29.2-002.json) —
# scripts/opencode-worktree-smoke.sh cut two concurrent git worktrees, each
# carrying the required opencode.json permission config, and both completed
# the requested edit with no cross-contamination. Unlike qwen's 29.1-001
# verified-negative, a `parallel` request routed to opencode must no longer
# degrade to serial. These tests lock the current (positive) degradation
# behaviour as a regression guard, mirroring test_qwen_adapter.py's
# verified-negative block but asserting the opposite outcome.
# ---------------------------------------------------------------------------


def test_opencode_parallel_request_no_longer_degrades_to_serial() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    capabilities = resolve_capabilities(opencode)
    plan = evaluate_degradations(opencode.name, capabilities, requested_mode=MODE_PARALLEL)

    assert not plan.has(DegradationKind.PARALLEL_TO_SERIAL)
    assert plan.effective_mode == MODE_PARALLEL


def test_opencode_preflight_does_not_warn_on_parallel_request() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    pf = preflight_harness(opencode, requested_mode=MODE_PARALLEL)

    assert not any("parallel" in warning for warning in pf.warnings)


def test_opencode_capabilities_declare_worktree_isolation_and_parallel() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    assert opencode.capabilities["worktree_isolation"] is True
    assert opencode.capabilities["parallel"] is True


# ---------------------------------------------------------------------------
# Story 29.2-003 AC2: usage_tracking flipped true once `opencode-json` could
# recover real usage — the degradation plan must stop recording
# `usage_unavailable` for opencode. Mirrors the block above's regression-guard
# style for the parallel/worktree_isolation flip in 29.2-002.
# ---------------------------------------------------------------------------


def test_opencode_no_longer_records_usage_unavailable_degradation() -> None:
    opencode = resolve_harness("opencode", config_path=CONFIG_PATH)
    capabilities = resolve_capabilities(opencode)
    plan = evaluate_degradations(opencode.name, capabilities, requested_mode=MODE_PARALLEL)

    assert not plan.has(DegradationKind.USAGE_UNAVAILABLE)
