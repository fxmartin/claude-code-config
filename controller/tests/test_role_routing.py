# ABOUTME: Tests for per-role harness routing — map each pipeline role to a harness (Story 20.2-001).
# ABOUTME: Covers map parsing, per-role resolution, default collapse, and unknown/disabled preflight failure.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.harness import DEFAULT_HARNESS
from sdlc.role_routing import (
    PIPELINE_ROLES,
    ROLE_ALIASES,
    RoleRoutingError,
    canonical_role,
    check_review_bridge,
    default_registry_path,
    default_reviewers_path,
    parse_role_harness_map,
    resolve_role_routing,
)

# The repo's checked-in default registry.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "harnesses.yaml"
REVIEWERS_PATH = Path(__file__).resolve().parents[1] / "config" / "adversarial-reviewers.yaml"


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Keep SDLC_AGENT_CMD out of these tests so the default slot is the builtin."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)


# ---------------------------------------------------------------------------
# Role catalog + aliasing
# ---------------------------------------------------------------------------


def test_pipeline_roles_are_the_controller_stages() -> None:
    assert PIPELINE_ROLES == ("build", "coverage", "review", "merge", "docs")


def test_qa_is_an_alias_for_coverage() -> None:
    assert ROLE_ALIASES["qa"] == "coverage"
    assert canonical_role("qa") == "coverage"


def test_canonical_role_is_case_insensitive() -> None:
    assert canonical_role("  Build ") == "build"


def test_canonical_role_rejects_unknown() -> None:
    with pytest.raises(RoleRoutingError, match="unknown pipeline role"):
        canonical_role("deploy")


# ---------------------------------------------------------------------------
# Map parsing
# ---------------------------------------------------------------------------


def test_parse_map_canonicalizes_qa_to_coverage() -> None:
    assert parse_role_harness_map("build=claude,review=codex,qa=codex") == {
        "build": "claude",
        "review": "codex",
        "coverage": "codex",
    }


def test_parse_map_ignores_surrounding_whitespace() -> None:
    assert parse_role_harness_map(" build = claude , review = codex ") == {
        "build": "claude",
        "review": "codex",
    }


def test_parse_map_empty_spec_is_empty_map() -> None:
    assert parse_role_harness_map("") == {}
    assert parse_role_harness_map("  ,  ") == {}


def test_parse_map_rejects_entry_without_equals() -> None:
    with pytest.raises(RoleRoutingError, match="expected role=harness"):
        parse_role_harness_map("build")


def test_parse_map_rejects_missing_harness() -> None:
    with pytest.raises(RoleRoutingError, match="missing a harness"):
        parse_role_harness_map("build=")


def test_parse_map_rejects_unknown_role() -> None:
    with pytest.raises(RoleRoutingError, match="unknown pipeline role"):
        parse_role_harness_map("deploy=codex")


def test_parse_map_rejects_conflicting_assignments() -> None:
    # qa and coverage both canonicalize to coverage; conflicting values fail fast.
    with pytest.raises(RoleRoutingError, match="conflicting"):
        parse_role_harness_map("coverage=claude,qa=codex")


def test_parse_map_allows_duplicate_consistent_assignment() -> None:
    assert parse_role_harness_map("coverage=codex,qa=codex") == {"coverage": "codex"}


# ---------------------------------------------------------------------------
# Per-role resolution (AC1)
# ---------------------------------------------------------------------------


def test_resolve_routes_each_role_to_its_harness() -> None:
    """AC1: each role dispatches to its assigned harness from the registry."""
    role_map = {"build": "claude", "review": "codex", "coverage": "codex"}
    resolved = resolve_role_routing(role_map, config_path=CONFIG_PATH)
    assert resolved["build"].name == DEFAULT_HARNESS
    assert resolved["build"].source == "builtin"
    assert resolved["review"].name == "codex"
    assert resolved["review"].source == "registry"
    assert resolved["coverage"].name == "codex"


def test_resolve_covers_every_pipeline_role() -> None:
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    assert set(resolved) == set(PIPELINE_ROLES)
    # Unmapped roles collapse to the default harness.
    assert resolved["merge"].name == DEFAULT_HARNESS
    assert resolved["coverage"].name == DEFAULT_HARNESS


def test_resolve_accepts_qa_alias_in_map() -> None:
    resolved = resolve_role_routing({"qa": "codex"}, config_path=CONFIG_PATH)
    assert resolved["coverage"].name == "codex"


# ---------------------------------------------------------------------------
# Default collapse (AC2)
# ---------------------------------------------------------------------------


def test_resolve_no_map_collapses_to_single_default_harness() -> None:
    """AC2: no map -> every role runs on the built-in claude default."""
    for role_map in (None, {}):
        resolved = resolve_role_routing(role_map)
        assert set(resolved) == set(PIPELINE_ROLES)
        assert all(h.name == DEFAULT_HARNESS for h in resolved.values())
        assert all(h.source == "builtin" for h in resolved.values())


def test_resolve_default_collapse_needs_no_registry() -> None:
    # The default slot never consults the registry, so config_path is irrelevant.
    resolved = resolve_role_routing({"build": "claude", "merge": "claude"})
    assert all(h.name == DEFAULT_HARNESS for h in resolved.values())


# ---------------------------------------------------------------------------
# Unknown / disabled harness preflight failure (AC3)
# ---------------------------------------------------------------------------


def test_resolve_unknown_harness_fails_fast() -> None:
    """AC3: a role mapped to an unknown harness fails fast with a clear message."""
    with pytest.raises(RoleRoutingError, match="review"):
        resolve_role_routing({"review": "nope"}, config_path=CONFIG_PATH)


def test_resolve_non_default_harness_without_registry_fails() -> None:
    with pytest.raises(RoleRoutingError, match="registry"):
        resolve_role_routing({"review": "codex"}, config_path=None)


def test_resolve_disabled_harness_fails_fast(tmp_path: Path) -> None:
    """AC3: a role mapped to a disabled harness fails fast (no half-run)."""
    cfg = tmp_path / "harnesses.yaml"
    cfg.write_text(
        "harnesses:\n"
        "  codex:\n"
        "    command: codex exec\n"
        "    parser: codex-exec\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    with pytest.raises(RoleRoutingError, match="disabled harness 'codex'"):
        resolve_role_routing({"review": "codex"}, config_path=cfg)


# ---------------------------------------------------------------------------
# Adversarial-reviewers bridge (Epic-08 coordination point)
# ---------------------------------------------------------------------------


def test_review_bridge_allows_enabled_reviewer() -> None:
    # codex is enabled in the checked-in adversarial-reviewers.yaml.
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    check_review_bridge(resolved, reviewers_path=REVIEWERS_PATH)  # no raise


def test_review_bridge_rejects_disabled_reviewer(tmp_path: Path) -> None:
    reviewers = tmp_path / "adversarial-reviewers.yaml"
    reviewers.write_text(
        "consensus: any_block_majority\n"
        "reviewers:\n"
        "  codex:\n"
        "    command: codex-adversarial-review.sh --pr-number {pr_number}\n"
        "    timeout_sec: 300\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    with pytest.raises(RoleRoutingError, match="disabled in adversarial-reviewers"):
        check_review_bridge(resolved, reviewers_path=reviewers)


def test_review_bridge_noop_when_review_harness_absent_from_reviewers(tmp_path: Path) -> None:
    # A review harness that is not a registered reviewer is fine (not a conflict).
    reviewers = tmp_path / "adversarial-reviewers.yaml"
    reviewers.write_text(
        "reviewers:\n"
        "  gemini:\n"
        "    command: gemini-review\n"
        "    timeout_sec: 300\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    check_review_bridge(resolved, reviewers_path=reviewers)  # no raise


def test_review_bridge_noop_when_reviewers_file_missing(tmp_path: Path) -> None:
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    check_review_bridge(resolved, reviewers_path=tmp_path / "absent.yaml")  # no raise


def test_review_bridge_noop_when_review_is_default() -> None:
    resolved = resolve_role_routing(None)
    check_review_bridge(resolved, reviewers_path=REVIEWERS_PATH)  # no raise


def test_review_bridge_noop_when_reviewers_path_is_none() -> None:
    # No reviewers registry to reconcile against -> nothing to check.
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    check_review_bridge(resolved, reviewers_path=None)  # no raise


def test_review_bridge_noop_when_no_review_role() -> None:
    # A resolved map without a 'review' role (nothing to reconcile) is a no-op,
    # even when a real reviewers file is supplied.
    check_review_bridge({}, reviewers_path=REVIEWERS_PATH)  # no raise


def test_review_bridge_noop_when_reviewers_file_malformed(tmp_path: Path) -> None:
    # A malformed reviewer registry is Epic-08's gate to flag, not ours: the
    # bridge swallows the loader's AdversarialError and stays a no-op.
    reviewers = tmp_path / "adversarial-reviewers.yaml"
    reviewers.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    resolved = resolve_role_routing({"review": "codex"}, config_path=CONFIG_PATH)
    check_review_bridge(resolved, reviewers_path=reviewers)  # no raise


# ---------------------------------------------------------------------------
# Default config-path helpers
# ---------------------------------------------------------------------------


def test_default_registry_path_points_at_checked_in_config() -> None:
    assert default_registry_path() == CONFIG_PATH


def test_default_reviewers_path_points_at_checked_in_config() -> None:
    assert default_reviewers_path() == REVIEWERS_PATH
