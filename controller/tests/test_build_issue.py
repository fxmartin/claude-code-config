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
