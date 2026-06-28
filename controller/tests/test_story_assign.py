# ABOUTME: Tests for the Epic-22 story/epic assignment engine.
# ABOUTME: Story 22.5-002 — single-story assign, epic cascade, fail-fast, idempotent re-assign (both hosts).

from __future__ import annotations

import pytest

from sdlc.build import Ledger
from sdlc.issue_host import GITHUB, GITLAB, Issue, IssueHostAdapter
from sdlc.story_assign import AssignError, assign, is_epic_target

# --- a recording, in-memory fake host ---------------------------------------


class FakeHost(IssueHostAdapter):
    """An in-memory host implementing the adapter interface for assign tests.

    ``assigned`` records every ``issue_assign`` call as ``(ref, user)`` so a test
    can prove the host was (or was not) written. ``known_users`` gates
    ``user_exists`` so the unknown-user fail-fast can be exercised. ``host`` is set
    per instance so the *same* engine logic runs against GitHub and GitLab.
    """

    cli = "fake"

    def __init__(self, host: str, known_users=("alice", "bob", "fx")) -> None:
        super().__init__(runner=lambda argv, timeout=None: None)
        self.host = host
        self.known_users = set(known_users)
        self.assigned: list[tuple[str, str]] = []

    # -- abstract verbs (only the ones the engine touches do real work) --
    def whoami(self) -> str:  # pragma: no cover - unused here
        return "fx"

    def ensure_ready(self) -> str:  # pragma: no cover - unused here
        return "fx"

    def user_exists(self, user: str) -> bool:
        return user in self.known_users

    def issue_create(self, title, body, labels=None, assignee=None):  # pragma: no cover
        raise NotImplementedError

    def issue_update(self, ref, title=None, body=None, labels=None):  # pragma: no cover
        raise NotImplementedError

    def issue_assign(self, ref, assignee):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        self.assigned.append((ref, assignee))
        return Issue(host=self.host, ref=ref, assignees=(assignee,))

    def issue_close(self, ref):  # pragma: no cover - unused here
        raise NotImplementedError

    def issue_find(self, marker):  # pragma: no cover - unused here
        return None


# --- fixtures ----------------------------------------------------------------


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _seed(ledger: Ledger, story_id: str) -> None:
    """Upsert a bare spec row for ``story_id`` (epic/feature derived from the id)."""
    feature = story_id.split("-", 1)[0]
    epic = feature.split(".", 1)[0]
    ledger.inventory_upsert_specs([(story_id, epic, feature, story_id, 3, "Medium")])


def _map(ledger: Ledger, story_id: str, host: str, ref: str) -> None:
    ledger.inventory_set_mapping(story_id, host, ref)


HOSTS = [GITHUB, GITLAB]


# --- target classification ---------------------------------------------------


@pytest.mark.parametrize(
    "target, is_epic",
    [
        ("epic-22", True),
        ("EPIC-7", True),
        ("22.5-002", False),
        ("22", False),
        ("nonsense", False),
    ],
)
def test_is_epic_target(target, is_epic) -> None:
    assert is_epic_target(target) is is_epic


# --- AC1: single-story assign sets the host assignee + caches owner ----------


@pytest.mark.parametrize("host", HOSTS)
def test_single_story_assign(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.5-002")
    _map(ledger, "22.5-002", host, "42")
    adapter = FakeHost(host)

    result = assign(adapter, ledger, "22.5-002", "alice")

    assert result.is_epic is False
    assert result.assigned == ["22.5-002"]
    assert result.unmapped == []
    # host was written through the adapter…
    assert adapter.assigned == [("42", "alice")]
    # …and the inventory owner cache reflects it.
    assert ledger.inventory_get_owner("22.5-002") == "alice"


# --- AC2: epic-id cascades to every story in that epic ----------------------


@pytest.mark.parametrize("host", HOSTS)
def test_epic_cascade_assigns_every_mapped_story(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    for sid, ref in [("22.1-001", "1"), ("22.2-003", "2"), ("22.5-002", "3")]:
        _seed(ledger, sid)
        _map(ledger, sid, host, ref)
    # a story from a *different* epic must not be swept in by the cascade.
    _seed(ledger, "21.1-001")
    _map(ledger, "21.1-001", host, "9")
    adapter = FakeHost(host)

    result = assign(adapter, ledger, "epic-22", "bob")

    assert result.is_epic is True
    assert result.assigned == ["22.1-001", "22.2-003", "22.5-002"]
    assert {r for r, _ in adapter.assigned} == {"1", "2", "3"}
    assert all(u == "bob" for _, u in adapter.assigned)
    # the other epic's story was left untouched.
    assert ("9", "bob") not in adapter.assigned
    assert ledger.inventory_get_owner("21.1-001") is None


# --- AC3: unknown user fails fast before any assignment ----------------------


@pytest.mark.parametrize("host", HOSTS)
def test_unknown_user_fails_fast(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.5-002")
    _map(ledger, "22.5-002", host, "42")
    adapter = FakeHost(host)

    with pytest.raises(AssignError) as exc:
        assign(adapter, ledger, "22.5-002", "ghost")

    assert "ghost" in str(exc.value)
    # nothing was assigned and the owner cache is untouched.
    assert adapter.assigned == []
    assert ledger.inventory_get_owner("22.5-002") is None


# --- AC3: an unmapped story is reported, never silently succeeded ------------


@pytest.mark.parametrize("host", HOSTS)
def test_unmapped_story_reported(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.5-002")  # spec present but never mirrored → no mapping
    adapter = FakeHost(host)

    result = assign(adapter, ledger, "22.5-002", "alice")

    assert result.assigned == []
    assert result.unmapped == ["22.5-002"]
    assert adapter.assigned == []


def test_cascade_reports_unmapped_but_assigns_the_rest(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.1-001")
    _map(ledger, "22.1-001", GITHUB, "1")
    _seed(ledger, "22.2-003")  # left unmapped
    adapter = FakeHost(GITHUB)

    result = assign(adapter, ledger, "epic-22", "alice")

    assert result.assigned == ["22.1-001"]
    assert result.unmapped == ["22.2-003"]
    assert adapter.assigned == [("1", "alice")]


# --- a mapping for a *different* host counts as unmapped on this host --------


def test_cross_host_mapping_is_unmapped(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.5-002")
    _map(ledger, "22.5-002", GITLAB, "5")  # mirrored to GitLab…
    adapter = FakeHost(GITHUB)  # …but we are assigning on GitHub.

    result = assign(adapter, ledger, "22.5-002", "alice")

    assert result.unmapped == ["22.5-002"]
    assert adapter.assigned == []


# --- idempotency: re-assigning the same user is a no-op ----------------------


@pytest.mark.parametrize("host", HOSTS)
def test_reassign_same_user_is_noop(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, "22.5-002")
    _map(ledger, "22.5-002", host, "42")
    adapter = FakeHost(host)

    first = assign(adapter, ledger, "22.5-002", "alice")
    second = assign(adapter, ledger, "22.5-002", "alice")

    assert first.assigned == ["22.5-002"]
    # the second pass writes nothing to the host — it is already owned.
    assert second.assigned == []
    assert second.already == ["22.5-002"]
    assert adapter.assigned == [("42", "alice")]  # only the first call wrote


# --- empty / malformed inputs fail fast -------------------------------------


def test_empty_user_fails_fast(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)
    with pytest.raises(AssignError):
        assign(adapter, ledger, "22.5-002", "  ")


def test_bad_target_fails_fast(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)
    with pytest.raises(AssignError):
        assign(adapter, ledger, "not-a-target", "alice")


def test_epic_with_no_stories_fails_fast(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)
    with pytest.raises(AssignError) as exc:
        assign(adapter, ledger, "epic-99", "alice")
    assert "epic-99" in str(exc.value)
