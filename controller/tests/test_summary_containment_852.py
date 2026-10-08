"""Issue #852: the summary stage must be read-only, hook-free, and guarded."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sdlc.dispatch import DENY_BASELINE_ENV, resolve_agent_cmd, resolve_deny_rules
from sdlc.fix_issue import _commits_ahead_of_origin


def _bash_denied(rules: list[str], command: str) -> bool:
    import fnmatch

    return any(
        fnmatch.fnmatch(command, r[len("Bash("):-1])
        for r in rules
        if r.startswith("Bash(")
    )


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_summary_denies_file_writing_tools(tool, monkeypatch) -> None:
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    assert tool in resolve_deny_rules("summary")


@pytest.mark.parametrize(
    "command",
    ["git add -A", "git commit -m x", "git push origin main", "git merge x", "go build ./cmd/acm"],
)
def test_summary_denies_commit_and_build_commands(command, monkeypatch) -> None:
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    assert _bash_denied(resolve_deny_rules("summary"), command)


def test_other_read_only_roles_keep_edit_tools(monkeypatch) -> None:
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    assert "Edit" not in resolve_deny_rules("review")


def test_summary_command_disables_user_hooks(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    cmd = resolve_agent_cmd(role="summary")
    assert cmd[cmd.index("--settings") + 1] == '{"disableAllHooks": true}'
    assert "--settings" not in resolve_agent_cmd(role="review")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(work, "config", k, v)
    (work / "a").write_text("a")
    _git(work, "add", "a")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "main")
    return work


def test_guard_clean_checkout_reports_nothing(clone: Path) -> None:
    assert _commits_ahead_of_origin(clone) == []


def test_guard_lists_unpushed_commit_on_main(clone: Path) -> None:
    (clone / "b").write_text("b")
    _git(clone, "add", "b")
    _git(clone, "commit", "-m", "fix: sneaky")
    ahead = _commits_ahead_of_origin(clone)
    assert len(ahead) == 1 and "sneaky" in ahead[0]


def test_guard_ignores_non_git_directory(tmp_path: Path) -> None:
    assert _commits_ahead_of_origin(tmp_path) == []
