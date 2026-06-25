# ABOUTME: Tests for the documentation-currency lens — behavior-change heuristic + finding/policy routing.
# ABOUTME: Story 18.3-001 — keep user-facing docs current with each story.

from __future__ import annotations

import pytest

from sdlc.doc_currency import (
    DOC_CURRENCY_ENV,
    DOC_CURRENCY_POLICY_ENV,
    Policy,
    analyze_diff,
    analyze_paths,
    doc_currency_enabled,
    is_behavior_changing,
    is_doc,
    paths_from_diff,
    policy_from_env,
)


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_DIFF = """\
diff --git a/skills/foo/run.py b/skills/foo/run.py
index 1111111..2222222 100644
--- a/skills/foo/run.py
+++ b/skills/foo/run.py
@@ -1 +1 @@
-old
+new
diff --git a/docs/old-name.md b/docs/new-name.md
similarity index 100%
rename from docs/old-name.md
rename to docs/new-name.md
diff --git a/added.sh b/added.sh
new file mode 100755
--- /dev/null
+++ b/added.sh
@@ -0,0 +1 @@
+echo hi
"""


def test_paths_from_diff_collects_touched_paths() -> None:
    paths = paths_from_diff(_DIFF)
    assert "skills/foo/run.py" in paths
    # Both sides of a rename are surfaced so a doc rename still counts as a doc touch.
    assert "docs/new-name.md" in paths
    assert "docs/old-name.md" in paths
    assert "added.sh" in paths
    # /dev/null is never a real path.
    assert "/dev/null" not in paths


def test_paths_from_diff_empty() -> None:
    assert paths_from_diff("") == []


def test_paths_from_diff_rename_without_git_header() -> None:
    # A rename line whose path was not pre-seeded by a `diff --git` header still
    # surfaces — covers the append branch for paths first seen on a rename line.
    fragment = "rename from notes/guide.md\nrename to notes/guide-v2.md\n"
    assert paths_from_diff(fragment) == ["notes/guide.md", "notes/guide-v2.md"]


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,category",
    [
        ("skills/telegram/SKILL.py", "skill"),
        ("plugins/autonomous-sdlc/skills/build-stories/story-parser.py", "skill"),
        ("hooks/pre-commit.sh", "hook"),
        ("controller/src/sdlc/cli.py", "cli"),
        ("setup.sh", "installer"),
        ("bootstrap.sh", "installer"),
        ("lib/nix-install.sh", "installer"),
    ],
)
def test_is_behavior_changing_categories(path: str, category: str) -> None:
    assert is_behavior_changing(path) == category


@pytest.mark.parametrize(
    "path",
    [
        "controller/tests/test_build.py",
        "controller/src/sdlc/doc_currency.py",  # internal module, not a user-facing verb
        "README.md",
        "docs/controller-architecture.md",
        "flake.lock",
        "controller/tests/conftest.py",
    ],
)
def test_is_behavior_changing_quiet_paths(path: str) -> None:
    assert is_behavior_changing(path) is None


@pytest.mark.parametrize(
    "path,expected",
    [
        ("README.md", True),
        ("docs/install-windows.md", True),
        ("skills/telegram/SKILL.md", True),
        ("controller/README.md", True),
        ("controller/src/sdlc/cli.py", False),
        ("CHANGELOG.md", False),  # Epic-05 owns it — never satisfies doc currency
        ("controller/CHANGELOG.md", False),
    ],
)
def test_is_doc(path: str, expected: bool) -> None:
    assert is_doc(path) is expected


# ---------------------------------------------------------------------------
# Analysis: findings, quiet paths, policy
# ---------------------------------------------------------------------------


def test_behavior_change_without_docs_emits_finding() -> None:
    result = analyze_paths(["skills/foo/run.py"], enabled=True)
    assert result.has_findings
    assert [f.category for f in result.findings] == ["skill"]
    f = result.findings[0]
    assert f.source_path == "skills/foo/run.py"
    assert f.doc_hint  # names a candidate stale doc
    assert f.reason  # one-line why


def test_one_finding_per_category() -> None:
    result = analyze_paths(
        ["skills/a/x.py", "skills/b/y.py", "controller/src/sdlc/cli.py"],
        enabled=True,
    )
    cats = sorted(f.category for f in result.findings)
    assert cats == ["cli", "skill"]


def test_behavior_change_with_doc_update_is_quiet() -> None:
    result = analyze_paths(["skills/foo/run.py", "README.md"], enabled=True)
    assert not result.has_findings


def test_docs_only_diff_is_quiet() -> None:
    result = analyze_paths(["README.md", "docs/foo.md"], enabled=True)
    assert not result.has_findings


def test_behavior_neutral_diff_is_quiet() -> None:
    result = analyze_paths(
        ["controller/tests/test_build.py", "controller/src/sdlc/reconcile.py"],
        enabled=True,
    )
    assert not result.has_findings


def test_changelog_does_not_satisfy_doc_currency() -> None:
    # A behavior change shipped with only a CHANGELOG bump is still flagged: the
    # CHANGELOG is Epic-05's, not a user-facing doc, so it never makes docs current.
    result = analyze_paths(["skills/foo/run.py", "CHANGELOG.md"], enabled=True)
    assert result.has_findings


def test_changelog_only_diff_is_quiet() -> None:
    result = analyze_paths(["CHANGELOG.md"], enabled=True)
    assert not result.has_findings


# ---------------------------------------------------------------------------
# Disable switch
# ---------------------------------------------------------------------------


def test_disabled_returns_no_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DOC_CURRENCY_ENV, "off")
    result = analyze_paths(["skills/foo/run.py"])  # enabled resolved from env
    assert result.enabled is False
    assert not result.has_findings


def test_explicit_disabled_overrides_behavior_change() -> None:
    result = analyze_paths(["skills/foo/run.py"], enabled=False)
    assert result.enabled is False
    assert not result.has_findings


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_doc_currency_enabled_off_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv(DOC_CURRENCY_ENV, val)
    assert doc_currency_enabled() is False


@pytest.mark.parametrize("val", ["", "1", "true", "on", "anything"])
def test_doc_currency_enabled_default_on(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv(DOC_CURRENCY_ENV, val)
    assert doc_currency_enabled() is True


# ---------------------------------------------------------------------------
# Policy routing
# ---------------------------------------------------------------------------


def test_default_policy_is_advisory() -> None:
    result = analyze_paths(["skills/foo/run.py"], enabled=True)
    assert result.policy is Policy.ADVISORY
    assert result.route_to_bugfix is False


def test_route_to_bugfix_policy() -> None:
    result = analyze_paths(
        ["skills/foo/run.py"], enabled=True, policy=Policy.ROUTE_TO_BUGFIX
    )
    assert result.has_findings
    assert result.route_to_bugfix is True


def test_route_to_bugfix_only_when_findings_exist() -> None:
    # Policy set to route, but a quiet diff produces nothing to route.
    result = analyze_paths(
        ["README.md"], enabled=True, policy=Policy.ROUTE_TO_BUGFIX
    )
    assert result.route_to_bugfix is False


def test_policy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOC_CURRENCY_POLICY_ENV, raising=False)
    assert policy_from_env() is Policy.ADVISORY
    monkeypatch.setenv(DOC_CURRENCY_POLICY_ENV, "route_to_bugfix")
    assert policy_from_env() is Policy.ROUTE_TO_BUGFIX
    monkeypatch.setenv(DOC_CURRENCY_POLICY_ENV, "bogus")
    assert policy_from_env() is Policy.ADVISORY  # unknown falls back to advisory


def test_analyze_diff_end_to_end() -> None:
    diff = (
        "diff --git a/skills/foo/run.py b/skills/foo/run.py\n"
        "--- a/skills/foo/run.py\n"
        "+++ b/skills/foo/run.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    result = analyze_diff(diff, enabled=True)
    assert result.has_findings
    assert result.findings[0].category == "skill"


def test_summary_is_human_readable() -> None:
    result = analyze_paths(["skills/foo/run.py"], enabled=True)
    summary = result.summary()
    assert "skill" in summary.lower()
    quiet = analyze_paths(["README.md"], enabled=True).summary()
    assert quiet  # non-empty even when clean


def test_summary_when_disabled() -> None:
    # A disabled lens summarizes its own off state rather than any verdict.
    result = analyze_paths(["skills/foo/run.py"], enabled=False)
    assert result.summary() == "documentation-currency lens disabled"


# ---------------------------------------------------------------------------
# Prompt wiring — build + review dimension, gated by the disable switch
# ---------------------------------------------------------------------------


def _story():
    from sdlc.cohort import Story

    return Story(
        "18.3-001",
        "Some story title",
        "18",
        "agent-output-quality",
        "epic-18-agent-output-quality.md",
        "Should",
        3,
        "py",
        [],
        False,
    )


def test_build_prompt_includes_docs_instruction_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DOC_CURRENCY_ENV, raising=False)
    from sdlc.build import BuildOptions, render_build_prompt

    prompt = render_build_prompt(_story(), BuildOptions())
    assert "user-facing docs" in prompt
    assert "same commit" in prompt.lower()
    assert "CHANGELOG" in prompt  # told not to touch it


def test_build_prompt_omits_docs_instruction_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DOC_CURRENCY_ENV, "off")
    from sdlc.build import BuildOptions, render_build_prompt

    prompt = render_build_prompt(_story(), BuildOptions())
    assert "user-facing docs" not in prompt


def test_review_prompt_includes_doc_currency_dimension_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DOC_CURRENCY_ENV, raising=False)
    from sdlc.build import render_review_prompt

    prompt = render_review_prompt(_story(), 7)
    assert "documentation-currency" in prompt.lower()
    assert "advisory" in prompt.lower()


def test_review_prompt_omits_dimension_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DOC_CURRENCY_ENV, "off")
    from sdlc.build import render_review_prompt

    prompt = render_review_prompt(_story(), 7)
    assert "documentation-currency" not in prompt.lower()
