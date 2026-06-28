# ABOUTME: Behavior tests for the `sdlc issues init` subcommand (Story 22.3-001).
# ABOUTME: Verifies full backfill, done→closed, resume, and no-stories guidance via the CLI.

from __future__ import annotations

from pathlib import Path

import sdlc.issue_host as issue_host
from sdlc.cli import app
from sdlc.issue_host import GITHUB, Issue, IssueHostAdapter, IssueHostError
from typer.testing import CliRunner

runner = CliRunner()

_EPIC_MD = """\
# Epic 22: github story mirror

##### Story 22.1-001: open story one
**Priority**: Should Have
**Story Points**: 3
**Risk Level**: Low

##### Story 22.2-001: shipped story
**Status**: Done
**Priority**: Should Have
**Story Points**: 5
**Risk Level**: High
"""


class FakeHost(IssueHostAdapter):
    cli = "fake"

    def __init__(self, host: str = GITHUB) -> None:
        super().__init__(runner=lambda argv, timeout=None: None)
        self.host = host
        self.issues: dict[str, dict] = {}
        self._next = 1

    def whoami(self) -> str:
        return "me"

    def ensure_ready(self) -> str:
        return "me"

    def issue_create(self, title, body, labels=None, assignee=None):
        ref = str(self._next)
        self._next += 1
        self.issues[ref] = {"title": title, "body": body,
                            "labels": list(labels or []), "state": "open"}
        return Issue(host=self.host, ref=ref, url=f"http://h/issues/{ref}",
                     title=title, state="open")

    def issue_update(self, ref, title=None, body=None, labels=None):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        if ref not in self.issues:
            raise IssueHostError(f"issue {ref} not found")
        if body is not None:
            self.issues[ref]["body"] = body
        return Issue(host=self.host, ref=ref, title=title)

    def issue_assign(self, ref, assignee):  # pragma: no cover - unused
        return Issue(host=self.host, ref=str(ref), assignees=(assignee,))

    def issue_close(self, ref):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        self.issues[ref]["state"] = "closed"
        return Issue(host=self.host, ref=ref, state="closed")

    def issue_find(self, marker):
        for ref, data in self.issues.items():
            if marker in (data.get("body") or ""):
                return Issue(host=self.host, ref=ref, title=data["title"],
                             state=data["state"])
        return None


def _seed_stories(root: Path, text: str = _EPIC_MD) -> None:
    story_dir = root / "docs" / "stories"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "epic-22-github-story-mirror.md").write_text(text, encoding="utf-8")


def _patch_host(monkeypatch, fake: FakeHost) -> None:
    monkeypatch.setattr(issue_host, "resolve_host", lambda root, override=None: fake.host)
    monkeypatch.setattr(issue_host, "get_adapter", lambda host, runner=None: fake)


def test_init_backfills_and_reports(tmp_path, monkeypatch):
    _seed_stories(tmp_path)
    fake = FakeHost()
    _patch_host(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["issues", "init", "--host", "github",
         "--root", str(tmp_path), "--db", str(tmp_path / ".sdlc-state.db")],
    )

    assert result.exit_code == 0, result.output
    assert len(fake.issues) == 2  # one issue per story
    assert "2 story(ies) backfilled" in result.output
    assert "1 Done issue(s) closed" in result.output
    # the Done story's issue is closed on the host
    done_ref = next(r for r, d in fake.issues.items() if "22.2-001" in d["body"])
    assert fake.issues[done_ref]["state"] == "closed"


def test_init_resume_is_idempotent(tmp_path, monkeypatch):
    _seed_stories(tmp_path)
    fake = FakeHost()
    _patch_host(monkeypatch, fake)
    args = ["issues", "init", "--host", "github",
            "--root", str(tmp_path), "--db", str(tmp_path / ".sdlc-state.db")]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0 and second.exit_code == 0, second.output
    assert len(fake.issues) == 2  # no duplicates on re-run
    assert "2 updated" in second.output


def test_init_no_stories_points_to_generate_epics(tmp_path, monkeypatch):
    fake = FakeHost()
    _patch_host(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["issues", "init", "--host", "github", "--root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "generate-epics" in result.output
    assert len(fake.issues) == 0  # nothing provisioned on the host


def test_init_undeterminable_host_exits_two(tmp_path, monkeypatch):
    _seed_stories(tmp_path)

    def _boom(root, override=None):
        raise IssueHostError("could not determine code host from git remote")

    monkeypatch.setattr(issue_host, "resolve_host", _boom)

    result = runner.invoke(
        app, ["issues", "init", "--root", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "error" in result.output.lower()
    assert "code host" in result.output


def test_init_unauthenticated_cli_exits_two(tmp_path, monkeypatch):
    _seed_stories(tmp_path)

    class Unready(FakeHost):
        def ensure_ready(self):
            raise IssueHostError("not authenticated to github; run `gh auth login`")

    fake = Unready()
    _patch_host(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "init", "--host", "github", "--root", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "not authenticated" in result.output
