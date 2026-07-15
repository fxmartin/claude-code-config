# ABOUTME: Tests for build_issue — best-effort build-loop ↔ host-issue integration.
# ABOUTME: Story 22.4-002 — Closes #N close-link + live status comments/labels; never blocks a build.

from __future__ import annotations

import pytest

from sdlc import build_issue as bi
from sdlc import issue_host as ih
from sdlc.build import Ledger, BuildOptions, render_build_prompt, render_coverage_prompt
from sdlc.cohort import Story


# --- a recording fake runner (same shape as test_issue_host) -----------------


class FakeRunner:
    """Record argv and return canned RunResults keyed by an argv-substring needle."""

    def __init__(self, mapping=None, default=(0, "", "")):
        self.mapping = mapping or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.mapping.items():
            if needle in joined:
                rc, out, err = result
                return ih.RunResult(returncode=rc, stdout=out, stderr=err)
        rc, out, err = self.default
        return ih.RunResult(returncode=rc, stdout=out, stderr=err)


# --- fixtures ----------------------------------------------------------------


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _mapped(ledger: Ledger, story_id="22.4-002", host=ih.GITHUB, ref="42") -> None:
    """Project a spec row, then record its host issue mapping."""
    ledger.inventory_upsert_specs([(story_id, "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping(story_id, host, ref)


HOSTS = [ih.GITHUB, ih.GITLAB]


# --- close_link (AC1) --------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_close_link_for_mapped_story(tmp_path, host):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=host, ref="7")
    runner = FakeRunner()
    assert bi.close_link(ledger, "22.4-002", runner=runner) == "Closes #7"


def test_change_request_terms_match_mapped_host(tmp_path):
    """A story mapped to a host yields that host's CR terms (Story 23.2-001 AC1/AC2)."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="23.2-001", host=ih.GITLAB, ref="7")
    _mapped(ledger, story_id="22.4-002", host=ih.GITHUB, ref="9")
    assert bi.change_request_terms(ledger, "23.2-001") is ih.GITLAB_CR_TERMS
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_unmapped_defaults_to_github(tmp_path):
    """An unmapped story falls back to GitHub terms so its prompt is unchanged (AC2)."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_unsupported_host_defaults_to_github(tmp_path):
    """An unsupported recorded host degrades to GitHub terms, never raises."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping("22.4-002", "bitbucket", "9")
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_tolerates_broken_ledger():
    """A ledger stub lacking inventory_get_mapping must not crash the build."""
    class _NoInventory:
        pass

    assert bi.change_request_terms(_NoInventory(), "22.4-002") is ih.GITHUB_CR_TERMS  # type: ignore[arg-type]


def test_close_link_unmapped_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    # spec row exists but no host/issue_ref → unmapped.
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_no_row_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_unsupported_host_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping("22.4-002", "bitbucket", "9")
    # An unsupported host must never raise — best-effort yields None.
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_tolerates_broken_ledger():
    class _NoInventory:
        pass

    # A ledger stub lacking inventory_get_mapping must not crash the build.
    assert bi.close_link(_NoInventory(), "22.4-002") is None  # type: ignore[arg-type]


# --- announce_status (AC2) ---------------------------------------------------


def test_announce_status_posts_comment_and_label_github(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()

    applied = bi.announce_status(ledger, "22.4-002", "building", runner=runner)

    assert applied == "building"
    joined = [" ".join(c) for c in runner.calls]
    # A short comment on the issue, via the developer's own gh identity.
    assert any("gh issue comment 42" in c and "building" in c for c in joined)
    # A status:<slug> label stamped, prior status labels removed.
    edit = next(c for c in runner.calls if "edit" in c)
    assert "--add-label" in edit and "status:building" in edit
    assert "--remove-label" in edit and "status:in-review" in edit


@pytest.mark.parametrize("host,cli,comment_verb", [
    (ih.GITHUB, "gh", "comment"),
    (ih.GITLAB, "glab", "note"),
])
def test_announce_status_uses_host_comment_verb(tmp_path, host, cli, comment_verb):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=host, ref="5")
    runner = FakeRunner()
    bi.announce_status(ledger, "22.4-002", "in-review", runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any(f"{cli} issue {comment_verb} 5" in c for c in joined)


def test_announce_status_unmapped_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    runner = FakeRunner()
    assert bi.announce_status(ledger, "22.4-002", "building", runner=runner) is None
    assert runner.calls == []  # never touched the host


def test_announce_status_none_status_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger)
    runner = FakeRunner()
    assert bi.announce_status(ledger, "22.4-002", None, runner=runner) is None
    assert runner.calls == []


def test_announce_status_tolerates_host_failure(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    # Every host call fails — the build must continue regardless.
    runner = FakeRunner(default=(1, "", "boom"))
    # Does not raise; returns None on a fully-failed comment+label attempt.
    assert bi.announce_status(ledger, "22.4-002", "building", runner=runner) is None


def test_announce_status_comment_failure_does_not_block_label(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    # Comment fails but the label edit succeeds — independent best-effort lanes.
    runner = FakeRunner(mapping={"issue comment": (1, "", "no perms")})
    applied = bi.announce_status(ledger, "22.4-002", "building", runner=runner)
    assert applied == "building"
    assert any("edit" in c for c in runner.calls)


def test_announce_status_tolerates_broken_ledger():
    class _NoInventory:
        pass

    # A ledger stub lacking inventory_get_mapping must not crash the build — the
    # lookup itself raising is swallowed to a logged no-op (mirrors close_link).
    assert bi.announce_status(_NoInventory(), "22.4-002", "building") is None  # type: ignore[arg-type]


# --- stage / terminal status mapping ----------------------------------------


@pytest.mark.parametrize("stage,slug", [
    ("build", "building"),
    ("coverage", "building"),
    ("review", "in-review"),
    ("merge", "merging"),
    ("nonsense", None),
])
def test_stage_status_mapping(stage, slug):
    assert bi.stage_status(stage) == slug


def test_announce_terminal_needs_attention(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()
    bi.announce_terminal(ledger, "22.4-002", "NEEDS_ATTENTION", runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any("gh issue comment 42" in c and "needs-attention" in c for c in joined)


def test_announce_terminal_done_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()
    # DONE auto-closes via the merge's Closes #N — no separate terminal comment.
    bi.announce_terminal(ledger, "22.4-002", "DONE", runner=runner)
    assert runner.calls == []


# --- close-link is injected into the PR-opening prompts (AC1) ----------------


def _story() -> Story:
    return Story(
        id="22.4-002", title="Build-loop integration", epic_id="epic-22",
        epic_name="github-story-mirror", epic_file="docs/stories/epic-22.md",
        priority="Should", points=5, agent_type="python-backend-engineer",
    )


def test_build_prompt_includes_close_link_when_it_opens_the_pr():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=True)
    prompt = render_build_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" in prompt


def test_build_prompt_omits_close_link_when_coverage_opens_pr():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=False)
    # The build agent commits locally; coverage opens the PR, so no close-link here.
    prompt = render_build_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" not in prompt


def test_coverage_prompt_includes_close_link():
    story = _story()
    opts = BuildOptions(scope="epic-22")
    prompt = render_coverage_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" in prompt


def test_prompts_unchanged_without_close_link():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=True)
    assert "Closes #" not in render_build_prompt(story, opts)
    assert "Closes #" not in render_coverage_prompt(story, opts)


# --- change_request_checks (Story 25.1-001) ----------------------------------


def test_change_request_checks_unmapped_returns_none(tmp_path):
    """An unmapped story yields None so the merge re-check degrades to a no-op."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("25.1-001", "25", "25.1", "t", 5, "Should")])
    assert bi.change_request_checks(ledger, "25.1-001", 100, runner=FakeRunner()) is None


def test_change_request_checks_reads_github_view(tmp_path):
    import json

    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="25.1-001", host=ih.GITHUB, ref="42")
    payload = json.dumps({
        "labels": [{"name": "risk:high"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "High-risk file approval gate",
             "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    })
    runner = FakeRunner({"pr view": (0, payload, "")})
    view = bi.change_request_checks(ledger, "25.1-001", 100, runner=runner)
    assert view is not None
    assert view.labels == ("risk:high",)
    assert view.checks == (("High-risk file approval gate", ih.CR_FAILED),)
    # It queries the change request (PR #100), not the issue mapping ref.
    assert "100" in runner.calls[-1]


def test_change_request_checks_tolerates_host_failure(tmp_path):
    """A host error yields None — never raises — so a hiccup never parks a story."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="25.1-001", host=ih.GITHUB, ref="42")
    runner = FakeRunner(default=(1, "", "boom"))
    assert bi.change_request_checks(ledger, "25.1-001", 100, runner=runner) is None
