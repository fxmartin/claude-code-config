# ABOUTME: Tests for the harness capability probe and preflight decision (Story 20.5-001).
# ABOUTME: Covers capability resolution, optional probe command, and the missing-capability mode decision.

from __future__ import annotations

import pytest

from sdlc.capability import (
    CAPABILITY_KEYS,
    MODE_PARALLEL,
    MODE_SERIAL,
    HarnessPreflight,
    ProbeResult,
    ProbeStatus,
    preflight_harness,
    probe_harness,
    resolve_capabilities,
)
from sdlc.harness import HarnessConfig, resolve_harness


def _harness(
    name: str = "codex",
    *,
    capabilities: dict[str, bool] | None = None,
    probe: str | None = None,
) -> HarnessConfig:
    return HarnessConfig(
        name=name,
        command=f"{name} exec",
        parser="x",
        capabilities=capabilities or {},
        probe=probe,
    )


# ---------------------------------------------------------------------------
# Capability resolution
# ---------------------------------------------------------------------------


def test_resolve_capabilities_returns_all_canonical_keys() -> None:
    resolved = resolve_capabilities(_harness(capabilities={}))
    assert set(CAPABILITY_KEYS).issubset(resolved)


def test_resolve_capabilities_defaults_undeclared_to_false() -> None:
    """An undeclared capability is assumed ABSENT (conservative default)."""
    resolved = resolve_capabilities(_harness(capabilities={}))
    assert all(resolved[key] is False for key in CAPABILITY_KEYS)


def test_resolve_capabilities_honours_declared_flags() -> None:
    resolved = resolve_capabilities(
        _harness(capabilities={"parallel": True, "usage_tracking": False})
    )
    assert resolved["parallel"] is True
    assert resolved["usage_tracking"] is False
    # Still-undeclared canonical keys default to False.
    assert resolved["worktree_isolation"] is False


def test_resolve_capabilities_preserves_extra_keys() -> None:
    resolved = resolve_capabilities(_harness(capabilities={"custom_flag": True}))
    assert resolved["custom_flag"] is True


def test_resolve_capabilities_coerces_truthy_values_to_bool() -> None:
    resolved = resolve_capabilities(_harness(capabilities={"parallel": 1}))  # type: ignore[dict-item]
    assert resolved["parallel"] is True


# ---------------------------------------------------------------------------
# Probe command (optional CLI installed/authenticated check)
# ---------------------------------------------------------------------------


def test_probe_unknown_when_no_command_declared() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    result = probe_harness(_harness(probe=None), runner=runner)
    assert result.status is ProbeStatus.UNKNOWN
    # The runner is never invoked when there is no probe command.
    assert calls == []


def test_probe_available_on_zero_exit() -> None:
    result = probe_harness(
        _harness(probe="codex --version"), runner=lambda argv: (0, "codex 1.0")
    )
    assert result.status is ProbeStatus.AVAILABLE
    assert result.command == "codex --version"


def test_probe_unavailable_on_nonzero_exit() -> None:
    result = probe_harness(
        _harness(probe="codex --version"),
        runner=lambda argv: (127, "command not found: codex"),
    )
    assert result.status is ProbeStatus.UNAVAILABLE
    assert "not found" in result.detail


def test_probe_splits_command_into_argv() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, ""

    probe_harness(_harness(probe="codex login status --json"), runner=runner)
    assert seen == [["codex", "login", "status", "--json"]]


# ---------------------------------------------------------------------------
# Preflight mode decision (AC2): degrade rather than fail mid-run
# ---------------------------------------------------------------------------


def test_preflight_serial_is_never_degraded() -> None:
    pf = preflight_harness(_harness(capabilities={}), requested_mode=MODE_SERIAL)
    assert pf.effective_mode == MODE_SERIAL
    assert pf.degraded is False


def test_preflight_parallel_supported_when_capable() -> None:
    pf = preflight_harness(
        _harness(capabilities={"parallel": True, "worktree_isolation": True}),
        requested_mode=MODE_PARALLEL,
    )
    assert pf.effective_mode == MODE_PARALLEL
    assert pf.degraded is False
    assert pf.warnings == []


def test_preflight_parallel_degrades_without_worktree_isolation() -> None:
    pf = preflight_harness(
        _harness(capabilities={"parallel": True, "worktree_isolation": False}),
        requested_mode=MODE_PARALLEL,
    )
    assert pf.effective_mode == MODE_SERIAL
    assert pf.degraded is True
    assert any("worktree_isolation" in w for w in pf.warnings)


def test_preflight_parallel_degrades_without_parallel_capability() -> None:
    pf = preflight_harness(
        _harness(capabilities={"parallel": False, "worktree_isolation": True}),
        requested_mode=MODE_PARALLEL,
    )
    assert pf.effective_mode == MODE_SERIAL
    assert pf.degraded is True
    assert any("parallel" in w for w in pf.warnings)


def test_preflight_records_resolved_capabilities() -> None:
    pf = preflight_harness(_harness(capabilities={"json_contract": True}))
    assert pf.capabilities["json_contract"] is True
    assert pf.capabilities["worktree_isolation"] is False


def test_preflight_surfaces_unavailable_probe_as_warning() -> None:
    pf = preflight_harness(
        _harness(probe="codex --version"),
        requested_mode=MODE_SERIAL,
        probe_runner=lambda argv: (127, "command not found"),
    )
    assert pf.probe.status is ProbeStatus.UNAVAILABLE
    assert any("probe" in w for w in pf.warnings)


def test_preflight_log_lines_include_capability_summary() -> None:
    pf = preflight_harness(
        _harness(name="codex", capabilities={"parallel": True}),
        requested_mode=MODE_SERIAL,
    )
    joined = "\n".join(pf.log_lines())
    assert "codex" in joined
    assert "parallel" in joined


def test_preflight_log_lines_announce_degradation() -> None:
    pf = preflight_harness(
        _harness(capabilities={}),
        requested_mode=MODE_PARALLEL,
    )
    joined = "\n".join(pf.log_lines())
    assert MODE_SERIAL in joined


# ---------------------------------------------------------------------------
# Builtin Claude harness keeps every capability (no degradation)
# ---------------------------------------------------------------------------


def test_preflight_builtin_claude_supports_parallel(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    pf = preflight_harness(resolve_harness(), requested_mode=MODE_PARALLEL)
    assert pf.effective_mode == MODE_PARALLEL
    assert pf.degraded is False
    # The builtin claude harness declares no probe command.
    assert pf.probe.status is ProbeStatus.UNKNOWN


def test_probe_result_is_frozen() -> None:
    result = ProbeResult(status=ProbeStatus.UNKNOWN)
    with pytest.raises(Exception):
        result.status = ProbeStatus.AVAILABLE  # type: ignore[misc]


def test_preflight_is_frozen() -> None:
    pf = preflight_harness(_harness(), requested_mode=MODE_SERIAL)
    assert isinstance(pf, HarnessPreflight)
    with pytest.raises(Exception):
        pf.effective_mode = MODE_PARALLEL  # type: ignore[misc]
