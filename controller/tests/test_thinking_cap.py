# ABOUTME: Tests for the thinking-token cap + early-compaction config (Story 14.2-002).
# ABOUTME: A configured cap is parsed, recorded per run, restored on resume, and bound at dispatch.

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    _resolve_dispatch,
    parse_build_args,
    run_build,
)
from sdlc.dispatch import dispatch_agent
from sdlc.resume import _options_from_config

from test_build import FakeDispatcher, _sample_queue


# ---------------------------------------------------------------------------
# Argument parsing — --thinking-cap
# ---------------------------------------------------------------------------

def test_parse_thinking_cap() -> None:
    opts = parse_build_args(["epic-14", "--thinking-cap=4096"])
    assert opts.thinking_cap == 4096


def test_parse_thinking_cap_default_is_zero() -> None:
    assert parse_build_args(["epic-14"]).thinking_cap == 0


def test_parse_thinking_cap_negative_raises() -> None:
    with pytest.raises(ValueError, match="thinking-cap"):
        parse_build_args(["--thinking-cap=-1"])


# ---------------------------------------------------------------------------
# Per-run recording (and resume restore)
# ---------------------------------------------------------------------------

def test_thinking_cap_recorded_in_run_config(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, thinking_cap=8000,
    )
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    config = Ledger(db).run_config(result.run_id)
    assert config["thinking_cap"] == 8000


def test_no_thinking_cap_recorded_as_zero(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert Ledger(db).run_config(result.run_id)["thinking_cap"] == 0


def test_resume_restores_thinking_cap() -> None:
    opts = _options_from_config("epic-14", {"mode": "serial"}, {"thinking_cap": 6000})
    assert opts.thinking_cap == 6000


def test_resume_defaults_thinking_cap_for_legacy_runs() -> None:
    # A run that predates this field has no key — default to no cap (unchanged).
    opts = _options_from_config("epic-14", {"mode": "serial"}, {})
    assert opts.thinking_cap == 0


# ---------------------------------------------------------------------------
# _resolve_dispatch — binds the cap onto the real seam only
# ---------------------------------------------------------------------------

def test_resolve_dispatch_no_cap_returns_real_seam() -> None:
    opts = BuildOptions()
    assert _resolve_dispatch(None, opts) is dispatch_agent


def test_resolve_dispatch_binds_cap_on_real_seam() -> None:
    opts = BuildOptions(thinking_cap=4096)
    bound = _resolve_dispatch(None, opts)
    assert isinstance(bound, functools.partial)
    assert bound.func is dispatch_agent
    assert bound.keywords["thinking_cap"] == 4096


def test_resolve_dispatch_does_not_wrap_injected_fake() -> None:
    """An injected fake owns its signature — the cap is never bound onto it."""
    fake = FakeDispatcher()
    opts = BuildOptions(thinking_cap=4096)
    assert _resolve_dispatch(fake, opts) is fake
