# ABOUTME: Tests for the high-risk file pattern gate (Story 8.2-001).
# ABOUTME: Covers glob matching, default-config load, and additive per-repo overrides.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.risk_gate import (
    DEFAULT_CONFIG_PATH,
    RiskGateError,
    load_patterns,
    match_high_risk,
    matches_pattern,
)


class TestMatchesPattern:
    """The glob matcher must treat ``**`` as crossing path separators and
    ``*`` as matching within a single segment, mirroring gitignore-style
    globs used in the config."""

    def test_double_star_matches_nested_directory(self) -> None:
        assert matches_pattern("svc/migrations/0001_init.sql", "**/migrations/**")

    def test_double_star_matches_at_repo_root(self) -> None:
        assert matches_pattern("migrations/0001_init.sql", "**/migrations/**")

    def test_double_star_does_not_match_unrelated_path(self) -> None:
        assert not matches_pattern("src/app/main.py", "**/migrations/**")

    def test_extension_glob_matches(self) -> None:
        assert matches_pattern("infra/network.tf", "**/*.tf")

    def test_extension_glob_does_not_overmatch(self) -> None:
        assert not matches_pattern("infra/network.tfvars", "**/*.tf")

    def test_prefix_glob_matches_dockerfile_variants(self) -> None:
        assert matches_pattern("Dockerfile", "Dockerfile*")
        assert matches_pattern("Dockerfile.prod", "Dockerfile*")

    def test_single_star_stays_within_segment(self) -> None:
        # `*` must not cross a separator: a bare `*.sh` only matches root files.
        assert matches_pattern("deploy.sh", "*.sh")
        assert not matches_pattern("scripts/deploy.sh", "*.sh")

    def test_double_star_shell_matches_nested(self) -> None:
        assert matches_pattern("scripts/deploy.sh", "**/*.sh")


class TestLoadPatterns:
    def test_loads_default_config(self) -> None:
        patterns = load_patterns()
        assert "**/migrations/**" in patterns
        assert "**/auth/**" in patterns
        assert "Dockerfile*" in patterns

    def test_default_config_path_exists(self) -> None:
        assert DEFAULT_CONFIG_PATH.is_file()

    def test_override_is_additive(self, tmp_path: Path) -> None:
        override = tmp_path / ".sdlc-risk-config.yaml"
        override.write_text(
            "high_risk_patterns:\n  - '**/custom-secret/**'\n",
            encoding="utf-8",
        )
        patterns = load_patterns(override_path=override)
        # Override adds without removing the defaults.
        assert "**/custom-secret/**" in patterns
        assert "**/migrations/**" in patterns

    def test_override_does_not_duplicate(self, tmp_path: Path) -> None:
        override = tmp_path / ".sdlc-risk-config.yaml"
        override.write_text(
            "high_risk_patterns:\n  - '**/migrations/**'\n",
            encoding="utf-8",
        )
        patterns = load_patterns(override_path=override)
        assert patterns.count("**/migrations/**") == 1

    def test_missing_override_is_ignored(self, tmp_path: Path) -> None:
        patterns = load_patterns(override_path=tmp_path / "does-not-exist.yaml")
        assert "**/migrations/**" in patterns

    def test_malformed_config_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("high_risk_patterns: not-a-list\n", encoding="utf-8")
        with pytest.raises(RiskGateError):
            load_patterns(config_path=bad)


class TestMatchHighRisk:
    def test_returns_matched_files_with_patterns(self) -> None:
        changed = ["README.md", "svc/migrations/0001_init.sql", "infra/main.tf"]
        result = match_high_risk(changed)
        assert "svc/migrations/0001_init.sql" in result
        assert "infra/main.tf" in result
        assert "README.md" not in result

    def test_records_which_pattern_matched(self) -> None:
        result = match_high_risk(["svc/migrations/0001_init.sql"])
        assert result["svc/migrations/0001_init.sql"] == "**/migrations/**"

    def test_empty_changed_set_is_clean(self) -> None:
        assert match_high_risk([]) == {}

    def test_no_high_risk_files_returns_empty(self) -> None:
        assert match_high_risk(["README.md", "src/app/main.py"]) == {}

    def test_override_patterns_are_honored(self, tmp_path: Path) -> None:
        override = tmp_path / ".sdlc-risk-config.yaml"
        override.write_text(
            "high_risk_patterns:\n  - '**/special/**'\n",
            encoding="utf-8",
        )
        result = match_high_risk(["app/special/thing.py"], override_path=override)
        assert "app/special/thing.py" in result
