# ABOUTME: Epic-29 Story 29.1-001 — pins that the qwen parallel gate is exactly the two capability flags.
# ABOUTME: Proves flipping worktree_isolation+parallel is necessary AND sufficient; no evidence flip happens here.

from __future__ import annotations

from pathlib import Path

from sdlc.capability import MODE_PARALLEL, MODE_SERIAL
from sdlc.degradation import DegradationKind, evaluate_degradations
from sdlc.harness import load_harnesses_config

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "harnesses.yaml"
)


def test_qwen_registry_entry_has_the_expected_stable_shape() -> None:
    """The parts of the qwen entry that do NOT change when the flags flip."""
    registry = load_harnesses_config(CONFIG_PATH)
    assert "qwen" in registry
    qwen = registry["qwen"]
    assert qwen.parser == "codex-exec"
    assert qwen.probe == "qwen --version"
    assert qwen.capabilities["json_contract"] is True


def test_qwen_parallel_is_gated_solely_by_the_two_isolation_flags() -> None:
    """A qwen-shaped capability map degrades parallel→serial ONLY for the two
    flags, and flipping just those two removes the degradation entirely.

    This is the invariant Story 29.1-001 relies on: the evidence-gated flip of
    ``worktree_isolation`` + ``parallel`` in harnesses.yaml is both necessary
    (degradation present while false) and sufficient (gone once true) — nothing
    else in the qwen entry blocks parallel. It stays green after FX flips the
    real flags, because it evaluates constructed maps, not the checked-in YAML.
    """
    qwen_shape = {
        "worktree_isolation": False,
        "parallel": False,
        "json_contract": True,
        "usage_tracking": False,
        "rate_limit_aware": False,
    }

    gated = evaluate_degradations("qwen", qwen_shape, requested_mode=MODE_PARALLEL)
    assert gated.effective_mode == MODE_SERIAL
    assert gated.has(DegradationKind.PARALLEL_TO_SERIAL)
    missing = next(
        d for d in gated.degradations if d.kind is DegradationKind.PARALLEL_TO_SERIAL
    ).missing
    # The only unmet requirements are the two flags the evidence run flips.
    assert set(missing) == {"worktree_isolation", "parallel"}

    flipped = {**qwen_shape, "worktree_isolation": True, "parallel": True}
    verified = evaluate_degradations("qwen", flipped, requested_mode=MODE_PARALLEL)
    assert verified.effective_mode == MODE_PARALLEL
    assert not verified.has(DegradationKind.PARALLEL_TO_SERIAL)


def test_qwen_flip_leaves_telemetry_degradations_untouched() -> None:
    """Flipping the isolation flags must NOT silently claim usage/rate-limit
    telemetry qwen still lacks — those degradations survive the flip."""
    flipped = {
        "worktree_isolation": True,
        "parallel": True,
        "json_contract": True,
        "usage_tracking": False,
        "rate_limit_aware": False,
    }
    plan = evaluate_degradations("qwen", flipped, requested_mode=MODE_PARALLEL)
    assert plan.has(DegradationKind.USAGE_UNAVAILABLE)
    assert plan.has(DegradationKind.RATE_LIMIT_SKIPPED)
