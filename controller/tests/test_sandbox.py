# ABOUTME: Tests for the optional container sandbox build-loop wiring (Story 13.4-002).
# ABOUTME: --sandbox is parsed, recorded per run, restored on resume, and bound at dispatch.

from __future__ import annotations

import functools
from pathlib import Path

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
# Argument parsing — --sandbox
# ---------------------------------------------------------------------------

def test_parse_sandbox_flag() -> None:
    assert parse_build_args(["epic-13", "--sandbox"]).sandbox is True


def test_parse_sandbox_default_is_off() -> None:
    assert parse_build_args(["epic-13"]).sandbox is False


# ---------------------------------------------------------------------------
# Per-run recording (and resume restore)
# ---------------------------------------------------------------------------

def test_sandbox_recorded_in_run_config(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, sandbox=True,
    )
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert Ledger(db).run_config(result.run_id)["sandbox"] is True


def test_no_sandbox_recorded_as_false(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert Ledger(db).run_config(result.run_id)["sandbox"] is False


def test_resume_restores_sandbox() -> None:
    opts = _options_from_config("epic-13", {"mode": "serial"}, {"sandbox": True})
    assert opts.sandbox is True


def test_resume_defaults_sandbox_for_legacy_runs() -> None:
    # A run that predates this field has no key — default to host path (unchanged).
    opts = _options_from_config("epic-13", {"mode": "serial"}, {}).sandbox
    assert opts is False


# ---------------------------------------------------------------------------
# Dispatch binding — only the real seam is bound, only when enabled
# ---------------------------------------------------------------------------

def test_resolve_dispatch_binds_sandbox_on_real_seam() -> None:
    opts = BuildOptions(sandbox=True)
    bound = _resolve_dispatch(None, opts, dispatch_agent)
    assert isinstance(bound, functools.partial)
    assert bound.keywords.get("sandbox") is True


def test_resolve_dispatch_no_sandbox_leaves_seam_unwrapped() -> None:
    opts = BuildOptions(sandbox=False)
    bound = _resolve_dispatch(None, opts, dispatch_agent)
    # No sandbox and no thinking cap → the real seam is returned unchanged.
    assert bound is dispatch_agent


def test_resolve_dispatch_injected_fake_is_untouched() -> None:
    opts = BuildOptions(sandbox=True)
    fake = FakeDispatcher()
    assert _resolve_dispatch(fake, opts, dispatch_agent) is fake
