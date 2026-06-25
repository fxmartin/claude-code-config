# ABOUTME: Tests for the bounded CI eval config (Story 18.1-003) — asserts the
# ABOUTME: in-CI subset is a small, valid, baseline-comparable slice of the full eval.

from __future__ import annotations

import json
from pathlib import Path

from sdlc.evaluate import load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVAL_DIR = _REPO_ROOT / "controller" / "eval"
_CI_CONFIG = _EVAL_DIR / "ci-config.yaml"
_FULL_CONFIG = _EVAL_DIR / "eval-config.yaml"
_BASELINE = _EVAL_DIR / "baseline.json"


def test_ci_config_exists() -> None:
    assert _CI_CONFIG.is_file(), f"missing bounded CI eval config: {_CI_CONFIG}"


def test_ci_config_loads_and_validates() -> None:
    # Must parse with the same loader the harness uses — a malformed config would
    # otherwise only blow up live in CI while spending quota.
    config = load_config(_CI_CONFIG)
    assert config.tickets, "CI config must have at least one ticket"


def test_ci_config_is_quota_bounded() -> None:
    # The CI subset spends real quota on Max, so it must stay tiny: a single ticket
    # at n=1 (the full eval keeps the larger ticket set × n for manual/local runs).
    config = load_config(_CI_CONFIG)
    assert config.n == 1, "CI eval must run n=1 to bound quota spend"
    assert len(config.tickets) == 1, "CI eval must run exactly one ticket to stay cheap"


def test_ci_config_reuses_the_shared_sample_target() -> None:
    # Same versioned sample target as the full eval — no separate fixture to drift.
    ci = load_config(_CI_CONFIG)
    full = load_config(_FULL_CONFIG)
    assert ci.target == full.target


def test_ci_ticket_is_a_subset_of_the_full_eval() -> None:
    # Every CI ticket must exist verbatim in the full eval so the CI run is a true
    # slice (same prompt + quality check), not a divergent ad-hoc ticket.
    ci = load_config(_CI_CONFIG)
    full = {t.id: t for t in load_config(_FULL_CONFIG).tickets}
    for ticket in ci.tickets:
        assert ticket.id in full, f"CI ticket {ticket.id!r} is not in the full eval"
        assert ticket.prompt == full[ticket.id].prompt
        assert ticket.quality_cmd == full[ticket.id].quality_cmd


def test_ci_ticket_is_present_in_the_committed_baseline() -> None:
    # eval-baseline compares the CI scoreboard against eval/baseline.json; the CI
    # ticket must have a baseline row or there is nothing to regression-check.
    ci = load_config(_CI_CONFIG)
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    baseline_ids = {t["ticket_id"] for t in baseline.get("tickets", [])}
    for ticket in ci.tickets:
        assert ticket.id in baseline_ids, (
            f"CI ticket {ticket.id!r} has no baseline row in {_BASELINE.name}"
        )
