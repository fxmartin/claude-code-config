# ABOUTME: Tests for identity — resolve the developer's login and cache owner/actor.
# ABOUTME: Story 22.5-001 — whoami both hosts, assignee→owner cache, no-auth degrades to `unknown`.

from __future__ import annotations

import pytest

from sdlc import issue_host as ih
from sdlc.build import Ledger
from sdlc.identity import (
    UNKNOWN_ACTOR,
    cache_actor,
    cache_owner,
    owner_from_issue,
    resolve_actor,
)
from sdlc.issue_host import GITHUB, GITLAB, Issue


# --- a recording fake runner (mirrors test_issue_host) -----------------------


class FakeRunner:
    """Return canned :class:`RunResult`s keyed by an argv-substring needle."""

    def __init__(self, mapping=None, default=(0, "", "")):
        self.mapping = mapping or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.mapping.items():
            if needle in joined:
                return _as_result(result)
        return _as_result(self.default)


def _as_result(value):
    if isinstance(value, ih.RunResult):
        return value
    rc, out, err = value
    return ih.RunResult(returncode=rc, stdout=out, stderr=err)


def _boom(argv, timeout=None):
    raise FileNotFoundError(argv[0])


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


# --- AC1: login resolution (both hosts) --------------------------------------


def test_resolve_actor_github_returns_login() -> None:
    runner = FakeRunner({"api user": (0, "octocat\n", "")})
    assert resolve_actor(ih.GitHubAdapter(runner=runner)) == "octocat"


def test_resolve_actor_gitlab_returns_login() -> None:
    runner = FakeRunner({"api user": (0, "rootuser\n", "")})
    assert resolve_actor(ih.GitLabAdapter(runner=runner)) == "rootuser"


# --- AC3: no host auth degrades gracefully (no crash) ------------------------


def test_resolve_actor_unauthenticated_degrades_to_unknown() -> None:
    # `gh api user` exits non-zero when not logged in → IssueHostError inside.
    runner = FakeRunner({"api user": (1, "", "not logged in")})
    assert resolve_actor(ih.GitHubAdapter(runner=runner)) == UNKNOWN_ACTOR


def test_resolve_actor_missing_cli_degrades_to_unknown() -> None:
    assert resolve_actor(ih.GitHubAdapter(runner=_boom)) == UNKNOWN_ACTOR


def test_resolve_actor_blank_login_degrades_to_unknown() -> None:
    runner = FakeRunner({"api user": (0, "   \n", "")})
    assert resolve_actor(ih.GitHubAdapter(runner=runner)) == UNKNOWN_ACTOR


# --- AC2: assignee → owner ---------------------------------------------------


def test_owner_from_issue_takes_first_assignee() -> None:
    issue = Issue(host=GITHUB, ref="7", assignees=("alice", "bob"))
    assert owner_from_issue(issue) == "alice"


def test_owner_from_issue_unassigned_is_none() -> None:
    assert owner_from_issue(Issue(host=GITHUB, ref="7")) is None


def test_owner_from_issue_none_is_none() -> None:
    assert owner_from_issue(None) is None


# --- actor cache: stamp each run in the ledger -------------------------------


def test_cache_actor_stamps_run(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("all", "serial")
    runner = FakeRunner({"api user": (0, "octocat\n", "")})

    actor = cache_actor(ledger, run_id, ih.GitHubAdapter(runner=runner))

    assert actor == "octocat"
    assert ledger.run_get_actor(run_id) == "octocat"


def test_cache_actor_no_auth_stamps_unknown(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("all", "serial")

    actor = cache_actor(ledger, run_id, ih.GitHubAdapter(runner=_boom))

    assert actor == UNKNOWN_ACTOR
    assert ledger.run_get_actor(run_id) == UNKNOWN_ACTOR


def test_run_get_actor_unset_is_none(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("all", "serial")
    assert ledger.run_get_actor(run_id) is None


def test_run_set_actor_preserves_run_fields(tmp_path) -> None:
    # actor is its own writer — stamping it must not disturb scope/mode/status.
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("all", "serial")
    before = ledger.run_row(run_id)
    assert before is not None

    ledger.run_set_actor(run_id, "octocat")

    after = ledger.run_row(run_id)
    assert after is not None
    assert after["actor"] == "octocat"
    for field in ("scope", "mode", "status"):
        assert after[field] == before[field]


def test_run_set_actor_restamp_overwrites(tmp_path) -> None:
    # A degraded first stamp can be corrected once host auth returns.
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("all", "serial")

    ledger.run_set_actor(run_id, UNKNOWN_ACTOR)
    ledger.run_set_actor(run_id, "octocat")

    assert ledger.run_get_actor(run_id) == "octocat"


# --- owner cache: a local build can show/skip by owner without an API call ---


@pytest.mark.parametrize("host", [GITHUB, GITLAB])
def test_cache_owner_records_assignee(tmp_path, host) -> None:
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.5-001", "22", "22.5", "Identity", 3, "Medium")])
    issue = Issue(host=host, ref="7", assignees=("alice",))

    owner = cache_owner(ledger, "22.5-001", issue)

    assert owner == "alice"
    assert ledger.inventory_get_owner("22.5-001") == "alice"


def test_cache_owner_unassigned_clears_owner(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.5-001", "22", "22.5", "Identity", 3, "Medium")])
    issue = Issue(host=GITHUB, ref="7", assignees=("alice",))
    cache_owner(ledger, "22.5-001", issue)

    # Re-sync after the assignee was removed on the host → owner cache clears.
    cache_owner(ledger, "22.5-001", Issue(host=GITHUB, ref="7"))

    assert ledger.inventory_get_owner("22.5-001") is None


def test_cache_owner_resyncs_to_new_assignee(tmp_path) -> None:
    # Reassignment on the host overwrites the cached owner on the next sync.
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.5-001", "22", "22.5", "Identity", 3, "Medium")])
    cache_owner(ledger, "22.5-001", Issue(host=GITHUB, ref="7", assignees=("alice",)))

    owner = cache_owner(
        ledger, "22.5-001", Issue(host=GITHUB, ref="7", assignees=("bob",))
    )

    assert owner == "bob"
    assert ledger.inventory_get_owner("22.5-001") == "bob"


def test_inventory_set_owner_preserves_mapping(tmp_path) -> None:
    # owner is its own writer — stamping it must not disturb host/issue_ref.
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.5-001", "22", "22.5", "Identity", 3, "Medium")])
    ledger.inventory_set_mapping("22.5-001", GITHUB, "7")

    ledger.inventory_set_owner("22.5-001", "alice")

    assert ledger.inventory_get_mapping("22.5-001") == (GITHUB, "7")
    assert ledger.inventory_get_owner("22.5-001") == "alice"


def test_inventory_get_owner_unmapped_is_none(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.5-001", "22", "22.5", "Identity", 3, "Medium")])
    assert ledger.inventory_get_owner("22.5-001") is None
