# ABOUTME: Tests for the Epic-22 field-directional reconcile (push managed + pull human).
# ABOUTME: Story 22.4-001 — push spec/status, pull assignee/labels, no-op idempotency, wontfix-skip (both hosts).

from __future__ import annotations

import pytest

from sdlc.build import Ledger
from sdlc.issue_host import GITHUB, GITLAB, Issue, IssueHostAdapter, IssueHostError
from sdlc.story_render import (
    MANAGED_OPEN,
    extract_managed_block,
    render_issue_body,
    story_marker,
)
from sdlc.story_sync import (
    NOOP,
    PUSHED,
    UNMAPPED,
    skip_for_human_status,
    status_label,
    sync_stories,
    sync_story,
)

# --- a recording, in-memory fake host ---------------------------------------


class FakeHost(IssueHostAdapter):
    """An in-memory host store implementing the adapter interface.

    Issues live in a ``ref -> {title, body, labels, state, assignees}`` dict so
    the engine's view/update calls hit a realistic store. ``host`` is set per
    instance so the *same* engine logic is exercised against GitHub and GitLab.
    Counters let the no-op tests assert the engine wrote nothing on a clean pass.
    """

    cli = "fake"

    def __init__(self, host: str) -> None:
        super().__init__(runner=lambda argv, timeout=None: None)
        self.host = host
        self.issues: dict[str, dict] = {}
        self._next = 1
        self.updated = 0

    def seed(self, body: str, labels=None, assignees=()) -> str:
        ref = str(self._next)
        self._next += 1
        self.issues[ref] = {
            "title": "t",
            "body": body,
            "labels": list(labels or []),
            "state": "open",
            "assignees": tuple(assignees),
        }
        return ref

    # -- abstract verbs --
    def whoami(self) -> str:  # pragma: no cover - unused here
        return "me"

    def ensure_ready(self) -> str:  # pragma: no cover - unused here
        return "me"

    def issue_create(self, title, body, labels=None, assignee=None):  # pragma: no cover
        ref = str(self._next)
        self._next += 1
        self.issues[ref] = {
            "title": title, "body": body, "labels": list(labels or []),
            "state": "open", "assignees": (assignee,) if assignee else (),
        }
        return Issue(host=self.host, ref=ref, title=title, state="open")

    def issue_update(self, ref, title=None, body=None, labels=None):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
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

    def issue_find(self, marker):  # pragma: no cover - unused here
        for ref, data in self.issues.items():
            if marker in (data.get("body") or ""):
                return Issue(host=self.host, ref=ref, title=data["title"])
        return None

    def issue_view(self, ref):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        if ref not in self.issues:
            raise IssueHostError(f"issue {ref} not found")
        data = self.issues[ref]
        return Issue(
            host=self.host, ref=ref, title=data["title"], state=data["state"],
            body=data["body"], labels=tuple(data["labels"]),
            assignees=tuple(data["assignees"]),
        )

    def issue_comment(self, ref, body):  # pragma: no cover - unused here
        pass


# --- fixtures ----------------------------------------------------------------


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _doc(story_id="22.4-001", title="Reconcile sync", spec="**User Story**: As a team, I want reconcile."):
    from sdlc.story_render import StoryDoc

    epic = story_id.split("-", 1)[0].split(".", 1)[0]
    feature = story_id.split("-", 1)[0]
    return StoryDoc(
        story_id=story_id, epic=epic, feature=feature, title=title,
        points=5, risk="High", spec_md=spec,
    )


def _seed_inventory(ledger: Ledger, doc) -> None:
    ledger.inventory_upsert_specs(
        [(doc.story_id, doc.epic, doc.feature, doc.title, doc.points, doc.risk)]
    )


def _map(ledger, adapter, doc, body, labels=None, assignees=()) -> str:
    """Seed an inventory row + a mapped host issue with the given body/labels."""
    _seed_inventory(ledger, doc)
    ref = adapter.seed(body=body, labels=labels, assignees=assignees)
    ledger.inventory_set_mapping(doc.story_id, adapter.host, ref)
    return ref


HOSTS = [GITHUB, GITLAB]


# --- AC1: push the managed spec block ----------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_push_regenerates_managed_block_md_wins(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc(spec="**User Story**: the real spec from the MD.")
    adapter = FakeHost(host)
    # the host issue carries a hand-edited managed block plus human discussion.
    tampered = (
        f"{MANAGED_OPEN}\n{story_marker(doc.story_id)}\n\nHAND EDITED — wrong.\n<!-- /managed -->\n\n"
        "## Discussion\nA human comment that must survive."
    )
    ref = _map(ledger, adapter, doc, body=tampered, labels=["story"])

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.action == PUSHED
    body = adapter.issues[ref]["body"]
    # MD content replaced the hand-edit inside the managed region...
    assert "the real spec from the MD." in extract_managed_block(body)
    assert "HAND EDITED" not in body
    # ...while human discussion *outside* the region is preserved.
    assert "A human comment that must survive." in body


@pytest.mark.parametrize("host", HOSTS)
def test_push_adds_taxonomy_and_status_label(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    ref = _map(ledger, adapter, doc, body=render_issue_body(doc), labels=["story"])
    # the build-owned cached execution status drives a status label on push.
    ledger.inventory_set_status(doc.story_id, "DONE")

    sync_story(adapter, ledger, doc)

    labels = adapter.issues[ref]["labels"]
    assert "epic:22" in labels and "feature:22.4" in labels
    assert "points:5" in labels and "risk:high" in labels
    assert status_label("DONE") in labels  # status:done


# --- AC2: pull assignee + human labels into the inventory --------------------


@pytest.mark.parametrize("host", HOSTS)
def test_pull_assignee_into_owner(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    _map(ledger, adapter, doc, body=render_issue_body(doc), labels=["story"],
         assignees=("alice",))

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.owner == "alice"
    assert ledger.inventory_get_owner(doc.story_id) == "alice"


@pytest.mark.parametrize("host", HOSTS)
def test_pull_human_status_label(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    _map(ledger, adapter, doc, body=render_issue_body(doc),
         labels=["story", "wontfix"])

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.human_status == "wontfix"
    assert ledger.inventory_get_human_status(doc.story_id) == "wontfix"


@pytest.mark.parametrize("host", HOSTS)
def test_pull_blocked_label(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    _map(ledger, adapter, doc, body=render_issue_body(doc),
         labels=["story", "blocked"])

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.human_status == "blocked"


# --- the build respects wontfix ----------------------------------------------


def test_wontfix_is_a_build_skip_signal(tmp_path):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(GITHUB)
    _map(ledger, adapter, doc, body=render_issue_body(doc),
         labels=["story", "wontfix"])

    sync_story(adapter, ledger, doc)

    # the build consults this signal and skips a wontfix story.
    assert skip_for_human_status(ledger.inventory_get_human_status(doc.story_id))
    assert doc.story_id in ledger.inventory_wontfix_story_ids()


def test_blocked_is_not_a_skip(tmp_path):
    # blocked is visible but does not skip the build (only wontfix does).
    assert not skip_for_human_status("blocked")
    assert not skip_for_human_status(None)
    assert skip_for_human_status("wontfix")


# --- AC3: repeated syncs with no changes are no-ops (no echo loop) ------------


@pytest.mark.parametrize("host", HOSTS)
def test_second_sync_is_a_noop(tmp_path, host):
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    # a stale issue (only the `story` label, no managed block) forces a first push.
    _map(ledger, adapter, doc, body="legacy body", labels=["story"],
         assignees=("alice",))

    first = sync_story(adapter, ledger, doc)
    assert first.action == PUSHED
    writes_after_first = adapter.updated
    assert writes_after_first == 1

    second = sync_story(adapter, ledger, doc)

    assert second.action == NOOP
    # the second pass wrote nothing to the host (no echo loop).
    assert adapter.updated == writes_after_first
    # pull still reflects the same owner — idempotent.
    assert ledger.inventory_get_owner(doc.story_id) == "alice"


@pytest.mark.parametrize("host", HOSTS)
def test_clean_issue_first_sync_is_noop(tmp_path, host):
    # an issue already carrying the current managed block and full taxonomy needs
    # no push at all — even the first sync is a no-op.
    ledger = _ledger(tmp_path)
    doc = _doc()
    adapter = FakeHost(host)
    _map(ledger, adapter, doc, body=render_issue_body(doc),
         labels=["story", "epic:22", "feature:22.4", "points:5", "risk:high"])

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.action == NOOP
    assert adapter.updated == 0


# --- unmapped stories are skipped (the mirror must run first) -----------------


def test_unmapped_story_is_skipped(tmp_path):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)  # projected but never mirrored → no mapping
    adapter = FakeHost(GITHUB)

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.action == UNMAPPED
    assert outcome.ref is None
    assert adapter.updated == 0


def test_mapping_on_other_host_is_skipped(tmp_path):
    ledger = _ledger(tmp_path)
    doc = _doc()
    _seed_inventory(ledger, doc)
    ledger.inventory_set_mapping(doc.story_id, GITLAB, "99")
    adapter = FakeHost(GITHUB)

    outcome = sync_story(adapter, ledger, doc)

    assert outcome.action == UNMAPPED


# --- batch helper ------------------------------------------------------------


def test_sync_stories_batches(tmp_path):
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)
    docs = [_doc("22.4-001", "A"), _doc("22.4-002", "B")]
    for d in docs:
        _map(ledger, adapter, d, body=render_issue_body(d),
             labels=["story", "epic:22", f"feature:{d.feature}", "points:5", "risk:high"])

    outcomes = sync_stories(adapter, ledger, docs)

    assert [o.action for o in outcomes] == [NOOP, NOOP]
    assert {o.story_id for o in outcomes} == {"22.4-001", "22.4-002"}


# --- status_label helper -----------------------------------------------------


def test_status_label_normalises():
    assert status_label("DONE") == "status:done"
    assert status_label("In Progress") == "status:in-progress"
    assert status_label(None) is None
    assert status_label("") is None
