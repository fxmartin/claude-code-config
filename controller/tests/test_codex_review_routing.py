# ABOUTME: Tests for routing review/QA to Codex through the unified registry (Story 20.3-002).
# ABOUTME: Covers the reviewer->harness link (SSOT), de-duplication gate, review-on-codex path, consensus parity.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.adversarial import (
    ReviewContext,
    ReviewerConfig,
    apply_consensus,
    dispatch_adversarial_review,
    load_reviewers_config,
)
from sdlc.build import parse_build_args, _stage_harness
from sdlc.harness import DEFAULT_HARNESS
from sdlc.role_routing import (
    RoleRoutingError,
    reconcile_reviewer_registry,
    resolve_role_routing,
    review_reviewer_for,
)

# The repo's checked-in registries — the ones a real run loads. Exercising the
# reconciliation against them (not a bespoke tmp file) is what proves AC1: the
# `review` role on Codex is governed by a single source of truth.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"
REVIEWERS_PATH = Path(__file__).resolve().parents[1] / "config" / "adversarial-reviewers.yaml"


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Keep SDLC_AGENT_CMD out of these tests so the default slot is the builtin."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)


def _write_reviewers(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "adversarial-reviewers.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _write_registry(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "harnesses.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The reviewer -> harness link (AC1: single source of truth)
# ---------------------------------------------------------------------------


def test_reviewer_config_parses_harness_link(tmp_path: Path) -> None:
    reviewers = _write_reviewers(
        tmp_path,
        "reviewers:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    timeout_sec: 300\n"
        "    enabled: true\n",
    )
    _consensus, parsed = load_reviewers_config(reviewers)
    assert parsed[0].harness == "codex"


def test_reviewer_harness_defaults_to_none_when_absent() -> None:
    # The gemini reviewer in the checked-in file declares no link.
    _consensus, reviewers = load_reviewers_config(REVIEWERS_PATH)
    by_name = {r.name: r for r in reviewers}
    assert by_name["gemini"].harness is None


def test_checked_in_codex_reviewer_is_a_view_over_the_harness_registry() -> None:
    """AC1: the Codex reviewer links to the `codex` harness in harnesses.yaml,
    so availability/identity has one source rather than two competing ones."""
    _consensus, reviewers = load_reviewers_config(REVIEWERS_PATH)
    by_name = {r.name: r for r in reviewers}
    assert by_name["codex"].harness == "codex"


# ---------------------------------------------------------------------------
# De-duplication gate: reconcile_reviewer_registry
# ---------------------------------------------------------------------------


def test_reconcile_passes_on_checked_in_configs() -> None:
    # The real configs are internally consistent: no raise.
    reconcile_reviewer_registry(
        registry_path=CONFIG_PATH, reviewers_path=REVIEWERS_PATH
    )


def test_reconcile_rejects_dangling_harness_link(tmp_path: Path) -> None:
    """A reviewer linking a harness absent from harnesses.yaml is exactly the
    'two competing Codex configurations' drift this story eliminates."""
    registry = _write_registry(
        tmp_path,
        "harnesses:\n"
        "  claude:\n"
        "    command: claude -p\n"
        "    parser: claude-stream-json\n",
    )
    reviewers = _write_reviewers(
        tmp_path,
        "reviewers:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    enabled: true\n",
    )
    with pytest.raises(RoleRoutingError, match="links harness 'codex'"):
        reconcile_reviewer_registry(
            registry_path=registry, reviewers_path=reviewers
        )


def test_reconcile_rejects_enabled_reviewer_linked_to_disabled_harness(
    tmp_path: Path,
) -> None:
    """An enabled Codex reviewer pointing at a Codex harness switched off in the
    registry is a divergent config — the single availability switch must win."""
    registry = _write_registry(
        tmp_path,
        "harnesses:\n"
        "  codex:\n"
        "    command: codex exec\n"
        "    parser: codex-exec\n"
        "    enabled: false\n",
    )
    reviewers = _write_reviewers(
        tmp_path,
        "reviewers:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    enabled: true\n",
    )
    with pytest.raises(RoleRoutingError, match="disabled"):
        reconcile_reviewer_registry(
            registry_path=registry, reviewers_path=reviewers
        )


def test_reconcile_allows_disabled_reviewer_linked_to_disabled_harness(
    tmp_path: Path,
) -> None:
    # Both off is consistent (Codex simply unavailable for review) — no raise.
    registry = _write_registry(
        tmp_path,
        "harnesses:\n"
        "  codex:\n"
        "    command: codex exec\n"
        "    parser: codex-exec\n"
        "    enabled: false\n",
    )
    reviewers = _write_reviewers(
        tmp_path,
        "reviewers:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    enabled: false\n",
    )
    reconcile_reviewer_registry(registry_path=registry, reviewers_path=reviewers)


def test_reconcile_ignores_unlinked_reviewers(tmp_path: Path) -> None:
    # A reviewer without a `harness:` link is not reconciled (legacy/standalone).
    registry = _write_registry(
        tmp_path,
        "harnesses:\n"
        "  claude:\n"
        "    command: claude -p\n"
        "    parser: claude-stream-json\n",
    )
    reviewers = _write_reviewers(
        tmp_path,
        "reviewers:\n"
        "  gemini:\n"
        "    command: gemini-review --pr {pr_url}\n"
        "    enabled: false\n",
    )
    reconcile_reviewer_registry(registry_path=registry, reviewers_path=reviewers)


def test_reconcile_noops_when_a_path_is_missing(tmp_path: Path) -> None:
    # Defensive parity with check_review_bridge: nothing to reconcile.
    reconcile_reviewer_registry(registry_path=None, reviewers_path=REVIEWERS_PATH)
    reconcile_reviewer_registry(registry_path=CONFIG_PATH, reviewers_path=None)
    reconcile_reviewer_registry(
        registry_path=CONFIG_PATH, reviewers_path=tmp_path / "absent.yaml"
    )


def test_reconcile_noops_on_malformed_files(tmp_path: Path) -> None:
    # A malformed registry/reviewer file is the owning gate's job to flag.
    bad = _write_reviewers(tmp_path, "- not\n- a\n- mapping\n")
    reconcile_reviewer_registry(registry_path=CONFIG_PATH, reviewers_path=bad)


# ---------------------------------------------------------------------------
# review_reviewer_for: which reviewer governs the review role (AC2)
# ---------------------------------------------------------------------------


def test_review_reviewer_for_returns_linked_codex_on_review_codex() -> None:
    """AC2: with review routed to Codex, the governing reviewer is the linked
    Codex entry from the single reviewer registry."""
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    reviewer = review_reviewer_for(resolved, reviewers_path=REVIEWERS_PATH)
    assert reviewer is not None
    assert reviewer.name == "codex"
    assert reviewer.harness == "codex"


def test_review_reviewer_for_none_when_review_is_default_claude() -> None:
    # No reviewer links the default claude harness, so the adversarial slot is
    # not the review path there — review_reviewer_for returns None.
    resolved = resolve_role_routing(None)
    assert resolved["review"].name == DEFAULT_HARNESS
    assert review_reviewer_for(resolved, reviewers_path=REVIEWERS_PATH) is None


def test_review_reviewer_for_none_when_reviewers_path_missing() -> None:
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    assert review_reviewer_for(resolved, reviewers_path=None) is None


def test_review_reviewer_for_none_when_reviewers_file_absent(tmp_path: Path) -> None:
    # A reviewers path that points at no file is a no-op: no reviewer can govern
    # the review role when the registry it would come from does not exist on disk.
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    missing = tmp_path / "adversarial-reviewers.yaml"
    assert review_reviewer_for(resolved, reviewers_path=missing) is None


def test_review_reviewer_for_none_when_reviewers_file_malformed(tmp_path: Path) -> None:
    # A malformed reviewer registry is the owning gate's job to flag, not this
    # resolver's — it swallows the load error and reports no governing reviewer.
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    bad = _write_reviewers(tmp_path, "- not\n- a\n- mapping\n")
    assert review_reviewer_for(resolved, reviewers_path=bad) is None


# ---------------------------------------------------------------------------
# Full review-on-codex path (AC2: review+qa on Codex, build on Claude)
# ---------------------------------------------------------------------------


def test_review_and_qa_on_codex_build_on_claude_is_recorded_per_stage() -> None:
    """AC2: `--harness review=codex,qa=codex,build=claude` routes review and QA
    to Codex and build to Claude, recorded per stage (Story 20.2-002)."""
    opts = parse_build_args(["--harness", "build=claude,review=codex,qa=codex"])
    assert _stage_harness("build", opts) == "claude"
    assert _stage_harness("review", opts) == "codex"
    assert _stage_harness("coverage", opts) == "codex"  # qa alias
    assert _stage_harness("merge", opts) == DEFAULT_HARNESS

    # And the same routing reconciles cleanly against the real registries.
    resolved = resolve_role_routing(
        opts.harness_map, config_path=CONFIG_PATH
    )
    assert resolved["review"].name == "codex"
    assert resolved["coverage"].name == "codex"
    reconcile_reviewer_registry(
        registry_path=CONFIG_PATH, reviewers_path=REVIEWERS_PATH
    )


# ---------------------------------------------------------------------------
# Consensus preserved (Epic-08 semantics untouched by the link)
# ---------------------------------------------------------------------------


def test_apply_consensus_unchanged_by_the_link() -> None:
    # The link is identity-only; the consensus reduction is exactly as before.
    assert apply_consensus(["approve", "approve"]) == "approve"
    assert apply_consensus(["approve", "block"]) == "block"
    assert apply_consensus([]) == "block"


def test_dispatch_round_trips_a_linked_reviewer(tmp_path: Path) -> None:
    """A reviewer carrying a `harness:` link still dispatches and reaches
    consensus exactly as Epic-08 specifies — the field is inert to dispatch."""
    reviewers = _write_reviewers(
        tmp_path,
        "consensus: any_block_majority\n"
        "reviewers:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    timeout_sec: 30\n"
        "    enabled: true\n",
    )

    def _fake_invoke(command: str, timeout: int) -> str:
        assert "codex-adversarial-review.sh" in command
        return (
            '{"reviewer_name": "codex", "verdict": "approve", '
            '"summary": "ok", "findings": []}'
        )

    result = dispatch_adversarial_review(
        pr_number=42,
        story_id="20.3-002",
        diff="diff --git ...",
        context=ReviewContext(tests_pass=True, coverage_pct=93.5, review_approved=True),
        pr_url="https://github.com/fxmartin/repo/pull/42",
        config_path=reviewers,
        invoke=_fake_invoke,
    )
    assert result.consensus == "approve"
    assert result.verdicts[0].reviewer_name == "codex"


def test_reviewer_config_carries_harness_field_default() -> None:
    # Direct construction keeps the new field optional (backward compatible).
    cfg = ReviewerConfig(
        name="codex",
        command="codex-adversarial-review.sh --pr-number {pr_number}",
        timeout_sec=300,
        enabled=True,
    )
    assert cfg.harness is None
