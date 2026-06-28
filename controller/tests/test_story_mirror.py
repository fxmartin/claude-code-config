# ABOUTME: Tests for the Epic-22 idempotent story ↔ host-issue mapping engine.
# ABOUTME: Story 22.2-003 — create-once, update-not-duplicate, marker-fallback, orphan re-create (both hosts).

from __future__ import annotations

import pytest

from sdlc.build import Ledger
from sdlc.issue_host import GITHUB, GITLAB, Issue, IssueHostAdapter, IssueHostError
from sdlc.story_mirror import (
    CREATED,
    RECOVERED,
    RECREATED,
    UPDATED,
    mirror_stories,
    mirror_story,
)
from sdlc.story_render import StoryDoc, story_marker

# --- a recording, in-memory fake host ---------------------------------------


class FakeHost(IssueHostAdapter):
    """An in-memory host store implementing the adapter interface.

    Issues live in a ``ref -> {title, body, labels, state}`` dict so the engine's
    create/update/find/close calls hit a realistic store. ``host`` is set per
    instance so the *same* engine logic is exercised against GitHub and GitLab.
    Deleting a ref models an issue removed on the host (orphan).
    """

    cli = "fake"

    def __init__(self, host: str) -> None:
        super().__init__(runner=lambda argv, timeout=None: None)
        self.host = host
        self.issues: dict[str, dict] = {}
        self._next = 1
        self.created = 0
        self.updated = 0

    # -- abstract verbs --
    def whoami(self) -> str:
        return "me"

    def ensure_ready(self) -> str:
        return "me"

    def issue_create(self, title, body, labels=None, assignee=None):
        ref = str(self._next)
        self._next += 1
        self.issues[ref] = {
            "title": title,
            "body": body,
            "labels": list(labels or []),
            "state": "open",
        }
        self.created += 1
        return Issue(host=self.host, ref=ref, url=f"http://h/issues/{ref}",
                     title=title, state="open")

    def issue_update(self, ref, title=None, body=None, labels=None):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        if ref not in self.issues:
            raise IssueHostError(f"issue {ref} not found")
        data = self.issues[ref]
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        for label in labels or []:
            if label not in data["labels"]:
                data["labels"].append(label)
        self.updated += 1
        return Issue(host=self.host, ref=ref, title=data["title"])

    def issue_assign(self, ref, assignee):  # pragma: no cover - unused here
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        return Issue(host=self.host, ref=ref, assignees=(assignee,))

    def issue_close(self, ref):  # pragma: no cover - unused here
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        self.issues[ref]["state"] = "closed"
        return Issue(host=self.host, ref=ref, state="closed")

    def issue_find(self, marker):
        for ref, data in self.issues.items():
            if marker in (data.get("body") or ""):
                return Issue(host=self.host, ref=ref,
                             url=f"http://h/issues/{ref}", title=data["title"],
                             state=data["state"])
        return None

    def issue_view(self, ref):  # pragma: no cover - unused here
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        data = self.issues[ref]
        return Issue(host=self.host, ref=ref, title=data["title"],
                     state=data["state"], body=data["body"],
                     labels=tuple(data["labels"]))


# --- fixtures ----------------------------------------------------------------


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _doc(story_id="22.2-003", title="Idempotent mapping") -> StoryDoc:
    epic = story_id.split("-", 1)[0].split(".", 1)[0]
    feature = story_id.split("-", 1)[0]
    return StoryDoc(
        story_id=story_id,
        epic=epic,
        feature=feature,
        title=title,
        points=5,
        risk="High",
        spec_md="**User Story**: As FX, I want one issue per story.",
    )


def _seed_inventory(ledger: Ledger, doc: StoryDoc) -> None:
    """Project the story's spec row first — the mirror records mapping onto it."""
    ledger.inventory_upsert_specs(
        [(doc.story_id, doc.epic, doc.feature, doc.title, doc.points, doc.risk)]
    )


HOSTS = [GITHUB, GITLAB]


# --- AC1: a story with no issue → create once --------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_creates_one_issue_and_records_mapping(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    adapter = FakeHost(host)

    outcome = mirror_story(adapter, ledger, doc)

    assert outcome.action == CREATED
    assert adapter.created == 1
    assert len(adapter.issues) == 1
    # mapping recorded in the inventory cache (host + issue_ref)
    assert ledger.inventory_get_mapping(doc.story_id) == (host, outcome.ref)
    # the hidden id marker is written into the issue body
    body = adapter.issues[outcome.ref]["body"]
    assert story_marker(doc.story_id) in body
    # carries the taxonomy `story` label
    assert "story" in adapter.issues[outcome.ref]["labels"]


# --- AC2: a story already mapped → update, never duplicate --------------------


@pytest.mark.parametrize("host", HOSTS)
def test_rerun_updates_does_not_duplicate(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    adapter = FakeHost(host)

    first = mirror_story(adapter, ledger, doc)
    second = mirror_story(adapter, ledger, doc)

    assert second.action == UPDATED
    assert second.ref == first.ref
    assert adapter.created == 1  # no second create
    assert adapter.updated == 1
    assert len(adapter.issues) == 1


# --- AC2 fallback: ref missing in inventory but marker matches on host --------


@pytest.mark.parametrize("host", HOSTS)
def test_marker_fallback_recovers_mapping(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    adapter = FakeHost(host)

    first = mirror_story(adapter, ledger, doc)
    # simulate the inventory mapping being lost (e.g. a gitignored ledger wiped),
    # while the issue with its marker still lives on the host.
    ledger.inventory_set_mapping(doc.story_id, None, None)
    assert ledger.inventory_get_mapping(doc.story_id) is None

    recovered = mirror_story(adapter, ledger, doc)

    assert recovered.action == RECOVERED
    assert recovered.ref == first.ref  # same issue re-discovered by marker
    assert adapter.created == 1  # no duplicate
    assert ledger.inventory_get_mapping(doc.story_id) == (host, first.ref)


# --- AC3: a mapped issue deleted on the host → orphan re-create ---------------


@pytest.mark.parametrize("host", HOSTS)
def test_orphan_recreated_when_issue_deleted(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    adapter = FakeHost(host)

    first = mirror_story(adapter, ledger, doc)
    # the issue is deleted on the host; the inventory still holds the stale ref.
    del adapter.issues[first.ref]

    outcome = mirror_story(adapter, ledger, doc)

    assert outcome.action == RECREATED
    assert outcome.ref != first.ref
    assert adapter.created == 2  # the orphan was re-created, not lost
    assert len(adapter.issues) == 1
    assert ledger.inventory_get_mapping(doc.story_id) == (host, outcome.ref)


# --- a mapping recorded for a different host is treated as unmapped here ------


def test_other_host_mapping_does_not_block_create(tmp_path):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    # pretend this story was previously mirrored to GitLab.
    ledger.inventory_set_mapping(doc.story_id, GITLAB, "99")
    adapter = FakeHost(GITHUB)

    outcome = mirror_story(adapter, ledger, doc)

    assert outcome.action == CREATED
    assert ledger.inventory_get_mapping(doc.story_id) == (GITHUB, outcome.ref)


# --- batch helper ------------------------------------------------------------


def test_mirror_stories_batches_and_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    docs = [_doc("22.2-003", "A"), _doc("22.3-001", "B")]
    for d in docs:
        _seed_inventory(ledger, d)
    adapter = FakeHost(GITHUB)

    first = mirror_stories(adapter, ledger, docs)
    assert [o.action for o in first] == [CREATED, CREATED]
    assert adapter.created == 2

    second = mirror_stories(adapter, ledger, docs)
    assert [o.action for o in second] == [UPDATED, UPDATED]
    assert adapter.created == 2  # still only two issues total
    assert len(adapter.issues) == 2


# --- Ledger mapping accessors: edge behaviours the engine relies on -----------


def test_get_mapping_returns_none_for_unseeded_story(tmp_path):
    """No inventory row at all → unmapped (the `row is None` branch)."""
    ledger = _ledger(tmp_path)
    assert ledger.inventory_get_mapping("99.9-999") is None


def test_get_mapping_treats_half_written_row_as_unmapped(tmp_path):
    """A row with only one of host/issue_ref set reads as unmapped.

    Story 22.2-003: a dangling ref must never be trusted — the engine recovers
    via the body marker instead. Both columns must be present to count.
    """
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)

    # host set, ref missing
    ledger.inventory_set_mapping(doc.story_id, GITHUB, None)
    assert ledger.inventory_get_mapping(doc.story_id) is None

    # ref set, host missing
    ledger.inventory_set_mapping(doc.story_id, None, "42")
    assert ledger.inventory_get_mapping(doc.story_id) is None


def test_set_mapping_clears_an_existing_mapping(tmp_path):
    """Passing None/None clears a recorded mapping (round-trip)."""
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)

    ledger.inventory_set_mapping(doc.story_id, GITHUB, "7")
    assert ledger.inventory_get_mapping(doc.story_id) == (GITHUB, "7")

    ledger.inventory_set_mapping(doc.story_id, None, None)
    assert ledger.inventory_get_mapping(doc.story_id) is None


def test_set_mapping_is_a_noop_when_story_row_absent(tmp_path):
    """The projector owns row creation — set_mapping never inserts.

    Story 22.2-003: a no-op when the story row is absent, so a mapping write for
    an unknown story neither raises nor conjures a row.
    """
    ledger = _ledger(tmp_path)

    ledger.inventory_set_mapping("404.0-001", GITHUB, "1")

    assert ledger.inventory_get_mapping("404.0-001") is None
    assert "404.0-001" not in ledger.inventory_story_ids()
