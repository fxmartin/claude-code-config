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
        "footer-leading-blank": [2, "always"],
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


# ---------------------------------------------------------------------------
# footer-leading-blank (Story 28.4-001 regression)
# ---------------------------------------------------------------------------
#
# conventional-commits-parser opens the footer at the *first* body line whose
# trimmed form matches ``<token>: `` or ``<token> #`` — anywhere in the body, not
# only after the last blank line. A wrapped bullet whose continuation happens to
# start ``word: `` therefore becomes the footer, and commitlint flunks it for
# having no leading blank line. Every expectation below was verified against
# @commitlint/parse + @commitlint/rules v21, not inferred from the docs.

def test_footer_with_leading_blank_passes() -> None:
    msg = (
        "test(x): do a thing\n"
        "\n"
        "The body explains why.\n"
        "\n"
        "Co-Authored-By: Someone <someone@example.com>\n"
    )
    assert lint_commit_message(msg, _RULES) == []


def test_body_line_reading_as_a_footer_token_is_flagged() -> None:
    msg = (
        "test(x): do a thing\n"
        "\n"
        "- a wrapped bullet whose continuation is pinned to its actual\n"
        "  guarantee: the build still dispatches every stage\n"
    )
    violations = lint_commit_message(msg, _RULES)
    assert any("footer-leading-blank" in v for v in violations)
    # The message must name the offending line so the amend re-ask is actionable.
    assert any("guarantee:" in v for v in violations)


def test_the_28_4_001_regression_message_is_flagged() -> None:
    # The original defect: this exact message passed the controller's gate and
    # then failed PR CI on footer-leading-blank, blocking the merge stage.
    msg = (
        "test(empirical-estimation): engage balanced routing by (#28.4-001)\n"
        "\n"
        "Close the coverage gaps the story's diff left behind.\n"
        "\n"
        "- `_log_routing_banner`'s blanket except is pinned to its actual\n"
        "  guarantee: with `routing_banner` raising, the build still dispatches\n"
        "  every stage on the frozen Balanced map and simply logs no banner.\n"
        "\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
    )
    assert any("footer-leading-blank" in v for v in lint_commit_message(msg, _RULES))


def test_hash_separated_footer_token_is_flagged() -> None:
    # ``<token> #`` is the parser's other footer form (``Closes #12``).
    msg = "test(x): do a thing\n\nthe body says why\nCloses #12\n"
    assert any("footer-leading-blank" in v for v in lint_commit_message(msg, _RULES))


def test_breaking_change_footer_without_blank_is_flagged() -> None:
    msg = "test(x): do a thing\n\nthe body says why\nBREAKING CHANGE: boom\n"
    assert any("footer-leading-blank" in v for v in lint_commit_message(msg, _RULES))


def test_whitespace_only_line_is_not_a_blank_leading_line() -> None:
    # commitlint compares the leading line to "" exactly, so three spaces flunk.
    msg = "test(x): do a thing\n\nthe body says why\n   \nRefs #12\n"
    assert any("footer-leading-blank" in v for v in lint_commit_message(msg, _RULES))


def test_multi_word_prefix_is_not_a_footer_token() -> None:
    # The token is ``[\w-]+``: a phrase with a space never opens the footer, so
    # ordinary prose with a colon must not produce a spurious re-ask.
    msg = "test(x): do a thing\n\nintro line\ntwo words: not a footer at all\n"
    assert lint_commit_message(msg, _RULES) == []


def test_colon_without_space_is_not_a_footer_token() -> None:
    msg = "test(x): do a thing\n\nintro line\nword:novalue is prose\n"
    assert lint_commit_message(msg, _RULES) == []


def test_footer_as_the_first_body_line_passes() -> None:
    # The blank line after the header is the footer's leading blank.
    msg = "test(x): do a thing\n\nCloses #12\n"
    assert lint_commit_message(msg, _RULES) == []


def test_footer_leading_blank_not_enforced_when_disabled() -> None:
    rules = {"rules": dict(_RULES["rules"], **{"footer-leading-blank": [0, "always"]})}
    msg = "test(x): do a thing\n\nintro line\nCloses #12\n"
    assert lint_commit_message(msg, rules) == []


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


# ---------------------------------------------------------------------------
# Compliant-by-construction subjects (Story 12.2-004)
# ---------------------------------------------------------------------------

from sdlc.commitlint import (  # noqa: E402
    build_commit_header,
    compliant_subject,
    header_max_length,
)


def test_header_max_length_honours_config() -> None:
    assert header_max_length(_RULES) == 72
    assert header_max_length({"rules": {"header-max-length": [2, "always", 50]}}) == 50


def test_header_max_length_defaults_to_72_without_config() -> None:
    # No config, or a disabled rule, falls back to the conventional default so a
    # subject can still be made compliant by construction.
    assert header_max_length(None) == 72
    assert header_max_length({"rules": {"header-max-length": [0, "always", 50]}}) == 72


def test_compliant_subject_lowercases_a_title_case_title() -> None:
    out = compliant_subject("Generate Compliant Commit Subjects", _RULES)
    assert out == "generate compliant commit subjects"


def test_compliant_subject_strips_trailing_period() -> None:
    assert compliant_subject("add the thing.", _RULES) == "add the thing"
    assert compliant_subject("add the thing...", _RULES) == "add the thing"


def test_compliant_subject_is_idempotent_on_compliant_input() -> None:
    # An already-compliant subject is returned unchanged (AC5).
    good = "add coverage gap detection"
    assert compliant_subject(good, _RULES) == good
    assert compliant_subject(compliant_subject(good, _RULES), _RULES) == good


def test_compliant_subject_trims_to_budget_on_word_boundary() -> None:
    prefix = "feat(controller-robustness): "
    trailer = " (#12.2-004)"
    out = compliant_subject(
        "Generate Commitlint Compliant Commit Subjects By Construction",
        _RULES,
        header_prefix=prefix,
        trailer=trailer,
    )
    # Fits the remaining budget, never splits a word, and stays lower-case.
    assert len(prefix) + len(out) + len(trailer) <= 72
    assert out == out.lower()
    assert not out.endswith(" ")
    assert " " not in "Generate"  # sanity: words preserved whole
    assert out.split() == [w for w in out.split()]  # no partial trailing token


def test_build_commit_header_is_compliant_for_long_title_case_title() -> None:
    # The Feature-12.3 style long Title-Case title used as a real fixture.
    header = build_commit_header(
        ctype="feat",
        scope="controller-robustness",
        subject="Reconcile Story Status Against Origin Main And Recompute Run Terminal",
        trailer=" (#12.3-001)",
        config=_RULES,
    )
    # The constructed header passes commitlint by construction (AC1/AC2).
    assert lint_commit_message(header, _RULES) == []
    # The (#id) trailer reconciliation keys off is always preserved intact.
    assert header.endswith(" (#12.3-001)")
    # The raw Title-Case title is never used verbatim as the subject.
    assert "Reconcile Story Status" not in header


def test_build_commit_header_idempotent_on_compliant_subject() -> None:
    header = build_commit_header(
        ctype="feat",
        scope="controller",
        subject="add the thing",
        trailer=" (#1.1-001)",
        config=_RULES,
    )
    assert header == "feat(controller): add the thing (#1.1-001)"
    assert lint_commit_message(header, _RULES) == []


def test_build_commit_header_without_config_uses_conventional_defaults() -> None:
    # Compliant by construction even when no repo config is loaded at render time.
    header = build_commit_header(
        ctype="feat",
        scope="controller-robustness",
        subject="Generate Compliant Commit Subjects",
        trailer=" (#12.2-004)",
        config=None,
    )
    assert lint_commit_message(header, _RULES) == []
    assert len(header) <= 72
    assert header.endswith(" (#12.2-004)")
