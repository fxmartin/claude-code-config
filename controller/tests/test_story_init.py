# ABOUTME: Tests for the Epic-22 full-backfill init engine (`sdlc issues init`).
# ABOUTME: Story 22.3-001 — backfill every story, done→closed, resume-idempotent, no-stories guidance.

from __future__ import annotations

import pytest

from sdlc.build import Ledger
from sdlc.issue_host import GITHUB, GITLAB, Issue, IssueHostAdapter, IssueHostError
from sdlc.story_init import (
    NoStoriesError,
    done_story_ids,
    init_issues,
)
from sdlc.story_mirror import CREATED, UPDATED
from sdlc.story_render import story_marker

# --- a recording, in-memory fake host ---------------------------------------


class FakeHost(IssueHostAdapter):
    """In-memory host store implementing the adapter interface.

    Issues live in a ``ref -> {title, body, labels, state}`` dict so the engine's
    create/update/find/close calls hit a realistic store. ``host`` is set per
    instance so the *same* init logic is exercised against GitHub and GitLab.
    ``issue_close`` is idempotent (closing a closed issue is a no-op), matching
    both `gh issue close` and `glab issue close`.
    """

    cli = "fake"

    def __init__(self, host: str) -> None:
        super().__init__(runner=lambda argv, timeout=None: None)
        self.host = host
        self.issues: dict[str, dict] = {}
        self._next = 1
        self.created = 0
        self.updated = 0
        self.closed = 0

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

    def issue_close(self, ref):
        ref = ref.ref if isinstance(ref, Issue) else str(ref)
        self.issues[ref]["state"] = "closed"
        self.closed += 1
        return Issue(host=self.host, ref=ref, state="closed")

    def issue_find(self, marker):
        for ref, data in self.issues.items():
            if marker in (data.get("body") or ""):
                return Issue(host=self.host, ref=ref,
                             url=f"http://h/issues/{ref}", title=data["title"],
                             state=data["state"])
        return None


# --- fixtures ----------------------------------------------------------------


HOSTS = [GITHUB, GITLAB]

# An epic with three stories: two still open, one marked Done via its Status
# line (the third's checked DoD also makes it shippable, double-belt).
_EPIC_MD = """\
# Epic 22: github story mirror

##### Story 22.1-001: open story one
**Priority**: Should Have
**Story Points**: 3
**Risk Level**: Low

##### Story 22.1-002: open story two
**Priority**: Must Have
**Story Points**: 2
**Risk Level**: Medium

##### Story 22.2-001: shipped story
**Status**: Done
**Priority**: Should Have
**Story Points**: 5
**Risk Level**: High

**Definition of Done**:
- [x] implemented
- [x] tested
"""


def _seed_stories(root, text: str = _EPIC_MD) -> None:
    story_dir = root / "docs" / "stories"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "epic-22-github-story-mirror.md").write_text(text, encoding="utf-8")


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


# --- done detection ----------------------------------------------------------


def test_done_story_ids_reads_status_and_dod(tmp_path):
    _seed_stories(tmp_path)
    assert done_story_ids(tmp_path) == {"22.2-001"}


def test_done_story_ids_empty_when_no_stories(tmp_path):
    assert done_story_ids(tmp_path) == set()


# --- AC1: full backfill — an issue for every story, mapping recorded ----------


@pytest.mark.parametrize("host", HOSTS)
def test_init_backfills_every_story_and_records_mapping(tmp_path, host):
    _seed_stories(tmp_path)
    ledger = _ledger(tmp_path)
    adapter = FakeHost(host)

    result = init_issues(adapter, ledger, root=tmp_path)

    assert result.host == host
    assert result.total == 3
    assert adapter.created == 3  # one issue per story, every epic
    # every story is mapped in the inventory (host + issue_ref)
    for story_id in ("22.1-001", "22.1-002", "22.2-001"):
        mapping = ledger.inventory_get_mapping(story_id)
        assert mapping is not None and mapping[0] == host
    # the spec rows were projected too (init owns provisioning end to end)
    assert {"22.1-001", "22.1-002", "22.2-001"} <= ledger.inventory_story_ids()
    # taxonomy carried on each issue
    for data in adapter.issues.values():
        assert "story" in data["labels"]
    assert all(o.action == CREATED for o in result.outcomes)


# --- AC2: a Done story → created AND immediately closed -----------------------


@pytest.mark.parametrize("host", HOSTS)
def test_init_closes_done_story_issues(tmp_path, host):
    _seed_stories(tmp_path)
    ledger = _ledger(tmp_path)
    adapter = FakeHost(host)

    result = init_issues(adapter, ledger, root=tmp_path)

    assert result.closed == ["22.2-001"]
    done_ref = ledger.inventory_get_mapping("22.2-001")[1]
    assert adapter.issues[done_ref]["state"] == "closed"
    # the marker is present on the closed issue — full history on the board
    assert story_marker("22.2-001") in adapter.issues[done_ref]["body"]
    # the still-open stories are not closed
    for sid in ("22.1-001", "22.1-002"):
        ref = ledger.inventory_get_mapping(sid)[1]
        assert adapter.issues[ref]["state"] == "open"


# --- AC3: interrupted/rate-limited → re-run resumes idempotently -------------


@pytest.mark.parametrize("host", HOSTS)
def test_init_resumes_without_duplicating(tmp_path, host):
    _seed_stories(tmp_path)
    ledger = _ledger(tmp_path)
    adapter = FakeHost(host)

    first = init_issues(adapter, ledger, root=tmp_path)
    assert adapter.created == 3

    second = init_issues(adapter, ledger, root=tmp_path)

    # no duplicates: still three issues, the second pass only updated them
    assert adapter.created == 3
    assert len(adapter.issues) == 3
    assert all(o.action == UPDATED for o in second.outcomes)
    # the Done story is still closed after a resume (update never reopens it)
    done_ref = ledger.inventory_get_mapping("22.2-001")[1]
    assert adapter.issues[done_ref]["state"] == "closed"
    assert second.closed == ["22.2-001"]
    assert first.total == second.total == 3


def test_init_resume_after_partial_interrupt(tmp_path):
    """A first pass that mirrored only some stories resumes the rest, no dupes."""
    _seed_stories(tmp_path)
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)

    # Simulate an interrupted first pass: only the first story got mirrored.
    from sdlc.story_mirror import mirror_story
    from sdlc.story_render import parse_story_docs

    docs = parse_story_docs(tmp_path)
    ledger.inventory_upsert_specs(
        [(d.story_id, d.epic, d.feature, d.title, d.points, d.risk) for d in docs]
    )
    mirror_story(adapter, ledger, docs[0])
    assert adapter.created == 1

    # Re-running init completes the backfill without duplicating the first issue.
    init_issues(adapter, ledger, root=tmp_path)

    assert adapter.created == 3
    assert len(adapter.issues) == 3


# --- AC4: no framework-format stories → clear guidance -----------------------


def test_init_no_stories_raises_guidance(tmp_path):
    ledger = _ledger(tmp_path)
    adapter = FakeHost(GITHUB)

    with pytest.raises(NoStoriesError) as exc:
        init_issues(adapter, ledger, root=tmp_path)

    assert "generate-epics" in str(exc.value)
    assert adapter.created == 0  # nothing provisioned on the host
