# ABOUTME: Unit tests for the commitlint validator (Story 12.2-002).
# ABOUTME: Covers config discovery + the faithful subset of conventional rules.

from __future__ import annotations

import json

from sdlc.commitlint import lint_commit_message, load_commitlint_config


# The repo's real ruleset, mirrored so the tests exercise the exact contract the
# controller lints against in production.
_RULES = {
    "rules": {
        "type-enum": [2, "always", ["feat", "fix", "chore", "docs", "test"]],
        "type-case": [2, "always", "lower-case"],
        "type-empty": [2, "never"],
        "scope-case": [2, "always", "lower-case"],
        "scope-empty": [0, "never"],
        "subject-empty": [2, "never"],
        "subject-case": [2, "always", "lower-case"],
        "subject-full-stop": [2, "never", "."],
        "header-max-length": [2, "always", 72],
        "body-leading-blank": [2, "always"],
    }
}


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------

def test_load_config_finds_commitlintrc_json(tmp_path) -> None:
    (tmp_path / ".commitlintrc.json").write_text(json.dumps(_RULES), encoding="utf-8")
    config = load_commitlint_config(tmp_path)
    assert config is not None
    assert config["rules"]["header-max-length"][2] == 72


def test_load_config_returns_none_when_absent(tmp_path) -> None:
    # Graceful no-op: no config → controller invents no rules.
    assert load_commitlint_config(tmp_path) is None


def test_load_config_ignores_malformed_json(tmp_path) -> None:
    (tmp_path / ".commitlintrc.json").write_text("{not json", encoding="utf-8")
    # A broken config is treated as "no config" rather than crashing the build.
    assert load_commitlint_config(tmp_path) is None


def test_load_config_reads_package_json_key(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "commitlint": _RULES}), encoding="utf-8"
    )
    config = load_commitlint_config(tmp_path)
    assert config is not None
    assert "type-enum" in config["rules"]


# ---------------------------------------------------------------------------
# A compliant message yields no violations
# ---------------------------------------------------------------------------

def test_compliant_message_passes() -> None:
    msg = "feat(controller): lint agent commits at commit time (#12.2-002)"
    assert lint_commit_message(msg, _RULES) == []


def test_compliant_message_with_body_passes() -> None:
    msg = "fix(cli): handle empty scope\n\nThe body explains why.\n"
    assert lint_commit_message(msg, _RULES) == []


# ---------------------------------------------------------------------------
# Individual rule violations
# ---------------------------------------------------------------------------

def test_header_too_long_flagged() -> None:
    subject = "x" * 80
    violations = lint_commit_message(f"feat: {subject}", _RULES)
    assert any("header-max-length" in v for v in violations)


def test_capitalized_subject_flagged() -> None:
    violations = lint_commit_message("feat: Add the thing", _RULES)
    assert any("subject-case" in v for v in violations)


def test_disallowed_type_flagged() -> None:
    violations = lint_commit_message("wibble: do a thing", _RULES)
    assert any("type-enum" in v for v in violations)


def test_uppercase_type_flagged() -> None:
    violations = lint_commit_message("Feat: do a thing", _RULES)
    assert any("type-case" in v for v in violations)


def test_uppercase_scope_flagged() -> None:
    violations = lint_commit_message("feat(CLI): do a thing", _RULES)
    assert any("scope-case" in v for v in violations)


def test_trailing_full_stop_flagged() -> None:
    violations = lint_commit_message("feat: do a thing.", _RULES)
    assert any("subject-full-stop" in v for v in violations)


def test_missing_type_and_subject_flagged() -> None:
    violations = lint_commit_message("just a sentence", _RULES)
    assert any("type-empty" in v for v in violations)


def test_body_not_blank_separated_flagged() -> None:
    violations = lint_commit_message("feat: do a thing\nno blank line", _RULES)
    assert any("body-leading-blank" in v for v in violations)


def test_the_10_2_001_regression_message_is_flagged() -> None:
    # The original defect: 84 chars + capitalized subject reached PR CI.
    msg = (
        "feat(coverage-orchestration): Add coverage gap detection and "
        "reporting for the build pipeline (#10.2-001)"
    )
    violations = lint_commit_message(msg, _RULES)
    assert any("header-max-length" in v for v in violations)
    assert any("subject-case" in v for v in violations)


# ---------------------------------------------------------------------------
# Disabled / unsupported rules
# ---------------------------------------------------------------------------

def test_disabled_rule_is_not_enforced() -> None:
    # scope-empty is level 0 in _RULES → a missing scope is fine.
    assert lint_commit_message("feat: no scope here", _RULES) == []


def test_unknown_rule_is_ignored() -> None:
    rules = {"rules": {"some-future-rule": [2, "always", "magic"]}}
    # The controller does not invent semantics for rules it does not understand.
    assert lint_commit_message("anything at all", rules) == []


def test_empty_rules_passes() -> None:
    assert lint_commit_message("Feat: WHATEVER.", {"rules": {}}) == []
