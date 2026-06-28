# ABOUTME: Tests for the Codex build/QA adapter (Story 20.3-001) — the registry
# ABOUTME: round-trip, the codex-exec parser selection, and the zero-claude property.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER
from sdlc.dispatch import AgentDispatchError
from sdlc.harness import dispatch_on_harness, resolve_harness
from sdlc.parsers import PlainResultParser, get_parser

# The checked-in registry — the one a real run loads. Exercising the adapter
# against it (rather than a bespoke tmp file) is what proves AC1: "harnesses.yaml
# has a codex entry ... a build dispatches a build/coverage agent to it".
CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"

_VALID_BUILD = {
    "branch_name": "feature/20.3-001",
    "build_status": "SUCCESS",
    "commit_sha": "deadbeef",
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
    """A Codex transcript ending in the harness-neutral result block."""
    body = json.dumps(payload)
    return f"codex prose and reasoning\n{RESULT_START_MARKER}\n{body}\n{RESULT_END_MARKER}\n"


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_codex_entry_uses_the_codex_exec_parser() -> None:
    """The registry's codex entry declares the no-telemetry codex-exec parser."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    assert codex.parser == "codex-exec"
    assert isinstance(get_parser(codex.parser), PlainResultParser)


def test_codex_argv_never_invokes_claude() -> None:
    """The codex harness renders its own command — no `claude` in the argv (AC3)."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    argv = codex.to_argv()
    assert argv, "codex harness rendered an empty command"
    assert not any("claude" in token for token in argv)


def test_shipped_codex_harness_pins_no_model_entitlement() -> None:
    """Issue #228: the shipped codex harness must run out of the box without
    assuming a specific model entitlement. Hardcoding `gpt-5.4-codex*` made every
    stage 400 on a ChatGPT-account Codex. The default must let Codex use the
    account's own configured model (no `--model` baked into the argv); per-stage
    model routing stays an opt-in a user enables with their own ids."""
    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    for stage in (None, "build", "coverage", "review", "merge", "adversarial"):
        argv = codex.to_argv() if stage is None else codex.to_argv(stage=stage)
        assert not any(
            token.startswith("gpt-") for token in argv
        ), f"shipped codex argv pins a model id for stage={stage}: {argv}"
        assert "--model" not in argv, (
            f"shipped codex argv forces a model for stage={stage}: {argv}"
        )


def test_build_agent_round_trips_through_codex(monkeypatch) -> None:
    """A build agent dispatched to codex returns the contract and validates (AC1/AC2)."""
    seen_cmd: list[str] = []
    seen_input: list[str | None] = []

    def fake_run(cmd, **kwargs):
        seen_cmd[:] = cmd
        seen_input.append(kwargs.get("input"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    result = dispatch_on_harness(codex, "build", "build story 20.3-001")

    assert result.data["build_status"] == "SUCCESS"
    assert result.data["commit_sha"] == "deadbeef"
    # The codex-exec parser records usage as unavailable, never fabricated (AC2/AC3).
    assert result.usage_available is False
    assert result.usage is None
    assert result.cost_usd is None
    # The agent ran on codex, not claude, and got the prompt on stdin.
    assert not any("claude" in token for token in seen_cmd)
    assert seen_input == ["build story 20.3-001"]


def test_coverage_agent_round_trips_through_codex(monkeypatch) -> None:
    """A coverage/QA agent dispatched to codex validates against its schema (AC1)."""
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeCompleted(_wrap(_VALID_COVERAGE))
    )

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    result = dispatch_on_harness(codex, "coverage", "qa story 20.3-001")

    assert result.data["coverage_status"] == "PASS"
    assert result.usage_available is False


def test_codex_nonzero_exit_is_plain_dispatch_error(monkeypatch) -> None:
    """A codex failure is a plain dispatch error — the parser never fabricates a 429."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted("", returncode=1, stderr="codex blew up"),
    )

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    with pytest.raises(AgentDispatchError) as excinfo:
        dispatch_on_harness(codex, "build", "prompt")
    # A plain AgentDispatchError, not a RateLimitError subclass.
    assert type(excinfo.value) is AgentDispatchError


# The zero-claude property is now asserted where it actually matters — against a
# recording dispatcher inside the *build loop* (Story 20.7-001), not on
# `harness.to_argv()` in isolation, which only ever proved the registry renders a
# claude-free template and never that a run dispatches to it. See
# tests/test_harness_routing_dispatch.py::
# test_full_codex_map_dispatches_codex_argv_for_every_stage.


def test_dispatch_on_harness_passes_through_dispatch_kwargs(monkeypatch) -> None:
    """Extra dispatch kwargs (e.g. cwd) reach dispatch_agent unchanged."""
    seen_cwd: list[object] = []

    def fake_run(cmd, **kwargs):
        seen_cwd.append(kwargs.get("cwd"))
        return _FakeCompleted(_wrap(_VALID_BUILD))

    monkeypatch.setattr(subprocess, "run", fake_run)

    codex = resolve_harness("codex", config_path=CONFIG_PATH)
    worktree = Path("/tmp/story-worktree")
    dispatch_on_harness(codex, "build", "prompt", cwd=worktree)

    assert seen_cwd == [worktree]


# --- The same seam, the *other* branch: a built-in / env slot is Claude under the
# hood, so dispatch_on_harness must select the dispatch default (stream-json)
# parser and decorate the Claude argv with the routed model — never the codex-exec
# parser. The codex tests above only ever exercise the registry branch, so without
# these the "default path is unchanged" promise in the docstring is untested.


def _capture_dispatch(monkeypatch) -> dict:
    """Swap dispatch_agent for a capturing stub; return the captured-kwargs dict."""
    captured: dict = {}

    def fake_dispatch(agent_type, prompt, *, agent_cmd=None, parser=None, **kwargs):
        captured.update(
            agent_type=agent_type,
            prompt=prompt,
            agent_cmd=agent_cmd,
            parser=parser,
            kwargs=kwargs,
        )
        return "dispatched"

    # dispatch_agent is imported into the harness namespace, so patch it there.
    monkeypatch.setattr("sdlc.harness.dispatch_agent", fake_dispatch)
    return captured


def test_builtin_slot_keeps_the_claude_default_parser(monkeypatch) -> None:
    """The built-in Claude slot dispatches with parser=None (the stream-json default)."""
    captured = _capture_dispatch(monkeypatch)

    builtin = resolve_harness()  # no name, no SDLC_AGENT_CMD -> built-in Claude
    assert builtin.source == "builtin"

    dispatch_on_harness(builtin, "build", "build on claude")

    # None -> dispatch picks the built-in Claude parser; the codex-exec parser is
    # never substituted onto the default slot.
    assert captured["parser"] is None
    # The argv is the Claude default command, not a registry template.
    assert "claude" in captured["agent_cmd"][0]


def test_builtin_slot_decorates_argv_with_routed_model(monkeypatch) -> None:
    """`model` flows into the built-in Claude argv via resolve_agent_cmd (AC: default path)."""
    captured = _capture_dispatch(monkeypatch)

    builtin = resolve_harness()
    dispatch_on_harness(builtin, "build", "prompt", model="opus")

    # resolve_agent_cmd appends `--model opus` for the default slot.
    argv = captured["agent_cmd"]
    assert argv[-2:] == ["--model", "opus"]


def test_env_override_slot_routes_through_the_default_parser(monkeypatch) -> None:
    """An SDLC_AGENT_CMD override is Claude under the hood — parser=None, no codex-exec."""
    captured = _capture_dispatch(monkeypatch)

    # The env slot's argv resolves through resolve_agent_cmd, which reads the real
    # environment — so set the override there, not via a throwaway dict.
    monkeypatch.setenv("SDLC_AGENT_CMD", "my-claude-wrapper --flag")
    override = resolve_harness()
    assert override.source == "env"

    dispatch_on_harness(override, "coverage", "qa prompt")

    assert captured["parser"] is None
    assert captured["agent_cmd"][0] == "my-claude-wrapper"
