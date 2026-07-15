# ABOUTME: Tests for deterministic change-class detection (Story 27.2-001).
# ABOUTME: Covers docs-pattern matching, the per-repo allowlist override, and the git diff feed.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sdlc.change_class import (
    CODE,
    DOCS_ONLY,
    OVERRIDE_FILENAME,
    ChangeClassError,
    changed_files,
    classify_files,
    load_docs_patterns,
)


# ---------------------------------------------------------------------------
# classify_files — the deterministic docs-only vs code verdict
# ---------------------------------------------------------------------------


def test_all_markdown_files_classify_docs_only() -> None:
    files = ["README.md", "docs/guide.md", "stories/epic-27.md"]
    assert classify_files(files) == DOCS_ONLY


def test_docs_tree_non_markdown_classifies_docs_only() -> None:
    # docs/** covers non-markdown assets under docs/ (images, diagrams).
    assert classify_files(["docs/img/pipeline.png", "docs/guide.md"]) == DOCS_ONLY


def test_any_code_file_classifies_code() -> None:
    files = ["README.md", "controller/src/sdlc/build.py"]
    assert classify_files(files) == CODE


def test_markdown_lookalike_extension_is_code() -> None:
    # `*.md` must not match `.mdx` or a file merely containing "md".
    assert classify_files(["site/page.mdx"]) == CODE
    assert classify_files(["cmd/main.go"]) == CODE


def test_empty_file_list_is_conservatively_code() -> None:
    # No verifiable diff → run the full gate chain, never skip on absence of
    # evidence.
    assert classify_files([]) == CODE


def test_explicit_patterns_override_defaults() -> None:
    assert classify_files(["notes.txt"], patterns=["*.txt"]) == DOCS_ONLY
    assert classify_files(["notes.txt", "a.py"], patterns=["*.txt"]) == CODE


# ---------------------------------------------------------------------------
# load_docs_patterns — defaults + additive per-repo allowlist
# ---------------------------------------------------------------------------


def test_default_patterns_cover_markdown_anywhere_and_docs_tree() -> None:
    patterns = load_docs_patterns()
    assert "**/*.md" in patterns
    assert "docs/**" in patterns


def test_override_file_is_additive(tmp_path: Path) -> None:
    (tmp_path / OVERRIDE_FILENAME).write_text(
        "docs_patterns:\n  - '*.rst'\n  - 'mkdocs.yml'\n", encoding="utf-8"
    )
    patterns = load_docs_patterns(root=tmp_path)
    # Defaults are kept — the allowlist extends, never replaces.
    assert "**/*.md" in patterns
    assert "*.rst" in patterns
    assert "mkdocs.yml" in patterns


def test_missing_override_file_yields_defaults(tmp_path: Path) -> None:
    assert load_docs_patterns(root=tmp_path) == load_docs_patterns()


def test_malformed_override_raises(tmp_path: Path) -> None:
    (tmp_path / OVERRIDE_FILENAME).write_text("docs_patterns: 42\n", encoding="utf-8")
    with pytest.raises(ChangeClassError):
        load_docs_patterns(root=tmp_path)


def test_invalid_yaml_override_raises(tmp_path: Path) -> None:
    # A syntactically broken allowlist fails loudly (ChangeClassError), never
    # silently — the caller then conservatively classifies as code.
    (tmp_path / OVERRIDE_FILENAME).write_text(
        "docs_patterns: [unclosed\n", encoding="utf-8"
    )
    with pytest.raises(ChangeClassError):
        load_docs_patterns(root=tmp_path)


def test_override_missing_key_raises(tmp_path: Path) -> None:
    (tmp_path / OVERRIDE_FILENAME).write_text("wrong_key: []\n", encoding="utf-8")
    with pytest.raises(ChangeClassError):
        load_docs_patterns(root=tmp_path)


def test_allowlisted_extra_pattern_flips_classification(tmp_path: Path) -> None:
    (tmp_path / OVERRIDE_FILENAME).write_text(
        "docs_patterns:\n  - 'CHANGELOG*'\n", encoding="utf-8"
    )
    patterns = load_docs_patterns(root=tmp_path)
    assert classify_files(["CHANGELOG.rst"], patterns=patterns) == DOCS_ONLY
    assert classify_files(["CHANGELOG.rst"]) == CODE


# ---------------------------------------------------------------------------
# changed_files — the deterministic git feed (not agent-reported)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    return root


def test_changed_files_lists_branch_diff(repo: Path) -> None:
    _git(repo, "checkout", "-b", "feature/x")
    (repo / "docs").mkdir()
    (repo / "docs" / "new.md").write_text("doc\n", encoding="utf-8")
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "docs change")
    files = changed_files(repo, "main", "feature/x")
    assert sorted(files) == ["README.md", "docs/new.md"]


def test_changed_files_unknown_ref_degrades_to_empty(repo: Path) -> None:
    assert changed_files(repo, "origin/main", "feature/does-not-exist") == []


def test_changed_files_non_repo_degrades_to_empty(tmp_path: Path) -> None:
    assert changed_files(tmp_path / "nowhere", "main", "feature/x") == []
