# ABOUTME: Tests for the `sdlc issues assign` CLI wiring (Story 22.5-002).
# ABOUTME: Drives assign via CliRunner with a stubbed code-host adapter (no live gh/glab).

from __future__ import annotations

from pathlib import Path

import sdlc.issue_host as ih
from sdlc.build import Ledger
from sdlc.cli import app
from typer.testing import CliRunner

from test_story_assign import FakeHost

runner = CliRunner()


def _seed_mapped(db: Path, story_id: str, host: str, ref: str) -> None:
    """Project a spec row for ``story_id`` and record its host issue mapping."""
    ledger = Ledger(db)
    ledger.init()
    feature = story_id.split("-", 1)[0]
    epic = feature.split(".", 1)[0]
    ledger.inventory_upsert_specs([(story_id, epic, feature, story_id, 3, "Medium")])
    ledger.inventory_set_mapping(story_id, host, ref)


def _patch_adapter(monkeypatch, fake: FakeHost) -> None:
    """Make the CLI's ``get_adapter`` return our in-memory fake host."""
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None: fake)


def test_assign_single_story(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_mapped(db, "22.5-002", ih.GITHUB, "42")
    fake = FakeHost(ih.GITHUB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "22.5-002", "alice", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert fake.assigned == [("42", "alice")]
    assert "1 assigned" in result.output
    assert Ledger(db).inventory_get_owner("22.5-002") == "alice"


def test_assign_epic_cascade(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    for sid, ref in [("22.1-001", "1"), ("22.5-002", "2")]:
        _seed_mapped(db, sid, ih.GITHUB, ref)
    fake = FakeHost(ih.GITHUB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "epic-22", "bob", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert {r for r, _ in fake.assigned} == {"1", "2"}
    assert "epic epic-22" in result.output


def test_assign_already_owned_is_noop(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_mapped(db, "22.5-002", ih.GITHUB, "42")
    # Pre-cache the owner so re-assigning the same user is the idempotent path.
    Ledger(db).inventory_set_owner("22.5-002", "alice")
    fake = FakeHost(ih.GITHUB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "22.5-002", "alice", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    # No host write — the story is already owned by alice.
    assert fake.assigned == []
    assert "1 already" in result.output
    assert "already alice" in result.output


def test_assign_unknown_user_exits_2(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_mapped(db, "22.5-002", ih.GITHUB, "42")
    fake = FakeHost(ih.GITHUB)  # known_users does not include "ghost"
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "22.5-002", "ghost", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 2
    assert "ghost" in result.output
    assert fake.assigned == []


def test_assign_unmapped_story_exits_1(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    # spec projected but never mirrored → no mapping.
    ledger = Ledger(db)
    ledger.init()
    ledger.inventory_upsert_specs([("22.5-002", "22", "22.5", "t", 3, "Medium")])
    fake = FakeHost(ih.GITHUB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "22.5-002", "alice", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 1
    assert "1 unmapped" in result.output
    assert fake.assigned == []


def test_assign_bad_target_exits_2(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).init()
    fake = FakeHost(ih.GITHUB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "nonsense", "alice", "--host", "github", "--db", str(db)]
    )

    assert result.exit_code == 2
    assert "nonsense" in result.output


def test_assign_unsupported_host_exits_2(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_mapped(db, "22.5-002", ih.GITHUB, "42")
    # No adapter patch needed — resolve_host rejects the host before adapter use.

    result = runner.invoke(
        app,
        ["issues", "assign", "22.5-002", "alice", "--host", "bitbucket", "--db", str(db)],
    )

    assert result.exit_code == 2
    assert "unsupported" in result.output.lower()


def test_assign_gitlab_host(tmp_path, monkeypatch) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_mapped(db, "22.5-002", ih.GITLAB, "5")
    fake = FakeHost(ih.GITLAB)
    _patch_adapter(monkeypatch, fake)

    result = runner.invoke(
        app, ["issues", "assign", "22.5-002", "alice", "--host", "gitlab", "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert fake.assigned == [("5", "alice")]
