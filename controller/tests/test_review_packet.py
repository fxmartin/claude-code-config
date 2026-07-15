# ABOUTME: Tests for the pre-baked review packet (Story 27.3-003).
# ABOUTME: Covers the builder, the size-cap fallback, the CLI verb, and prompt embedding.

"""Story 27.3-003 — pre-baked review packet.

The controller bakes a deterministic packet (PR meta, changed files, diff,
test/coverage signals) and embeds it into the review prompt so reviewers stop
re-deriving their inputs with ``gh pr view/diff/checkout`` round-trips. An
oversized or unbuildable packet falls back to today's fetch-it-yourself
instructions — a truncated diff is never injected.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sdlc import issue_host as ih
from sdlc import review_packet as rp

# --- fixtures ----------------------------------------------------------------

_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1,2 @@\n"
    " x = 1\n"
    "+y = 2\n"
    "diff --git a/docs/guide.md b/docs/guide.md\n"
    "--- a/docs/guide.md\n"
    "+++ b/docs/guide.md\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)

_META = ih.ChangeRequest(
    host="github",
    ref="7",
    url="https://github.com/fxmartin/repo/pull/7",
    title="feat(demo): add y",
    state="open",
    source_branch="feature/99.1-001",
    target_branch="main",
)


class FakeAdapter:
    """A minimal adapter double exposing the two verbs the builder consumes."""

    def __init__(self, meta=_META, diff=_DIFF, error=None):
        self.meta = meta
        self.diff = diff
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def cr_view(self, ref):
        self.calls.append(("cr_view", str(ref)))
        if self.error:
            raise self.error
        return self.meta

    def cr_diff(self, ref):
        self.calls.append(("cr_diff", str(ref)))
        if self.error:
            raise self.error
        return self.diff


# --- changed_files -----------------------------------------------------------


def test_changed_files_parses_post_image_paths() -> None:
    """Every `diff --git` header contributes its b/ (post-image) path, in order."""
    assert rp.changed_files(_DIFF) == ("src/app.py", "docs/guide.md")


def test_changed_files_takes_rename_destination() -> None:
    """A rename header contributes the destination path (what the reviewer reads)."""
    diff = "diff --git a/old/name.py b/new/name.py\n"
    assert rp.changed_files(diff) == ("new/name.py",)


def test_changed_files_dedupes_and_handles_empty() -> None:
    assert rp.changed_files("") == ()
    diff = (
        "diff --git a/x.py b/x.py\n@@\n"
        "diff --git a/x.py b/x.py\n@@\n"
    )
    assert rp.changed_files(diff) == ("x.py",)


# --- build_review_packet / render ---------------------------------------------


def test_build_review_packet_composes_meta_files_diff() -> None:
    adapter = FakeAdapter()
    packet = rp.build_review_packet(adapter, "7", checks="coverage_pct=93.4")
    assert packet.meta == _META
    assert packet.files == ("src/app.py", "docs/guide.md")
    assert packet.diff == _DIFF
    assert packet.checks == "coverage_pct=93.4"


def test_build_review_packet_empty_diff_raises() -> None:
    """An empty diff means there is nothing reviewable — never bake a hollow packet."""
    adapter = FakeAdapter(diff="   \n")
    with pytest.raises(ih.IssueHostError):
        rp.build_review_packet(adapter, "7")


def test_render_contains_meta_files_diff_and_checks() -> None:
    packet = rp.build_review_packet(FakeAdapter(), "7", checks="coverage_pct=93.4")
    text = packet.render()
    assert "#7" in text
    assert "feat(demo): add y" in text
    assert "https://github.com/fxmartin/repo/pull/7" in text
    assert "feature/99.1-001" in text and "main" in text
    assert "src/app.py" in text and "docs/guide.md" in text
    assert "+y = 2" in text
    assert "coverage_pct=93.4" in text


def test_render_names_missing_checks_signal() -> None:
    """No pipeline signals → the packet says so instead of omitting the section."""
    packet = rp.build_review_packet(FakeAdapter(), "7")
    assert "not available" in packet.render()


def test_render_fence_outgrows_backticks_in_diff() -> None:
    """A diff containing a ``` run gets a longer fence so the block never breaks."""
    diff = "diff --git a/x.md b/x.md\n+```python\n+code\n+```\n"
    packet = rp.build_review_packet(FakeAdapter(diff=diff), "7")
    assert "````" in packet.render()


# --- packet_block (best-effort, size-capped) -----------------------------------


def test_packet_block_returns_rendered_markdown() -> None:
    block = rp.packet_block(FakeAdapter(), "7", checks="tests green")
    assert block is not None
    assert "tests green" in block
    assert "+y = 2" in block


def test_packet_block_oversize_falls_back_never_truncates() -> None:
    """Over the cap the block is None (fallback) — never a shortened packet."""
    block = rp.packet_block(FakeAdapter(), "7", max_chars=50)
    assert block is None


def test_packet_block_host_error_falls_back() -> None:
    adapter = FakeAdapter(error=ih.IssueHostError("gh exploded"))
    assert rp.packet_block(adapter, "7") is None


# --- adapter cr_view (GitHub / GitLab parity) ----------------------------------


class _Runner:
    """Record argv and return a canned stdout."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        return ih.RunResult(returncode=0, stdout=self.stdout, stderr="")


def test_github_cr_view_parses_pr_meta() -> None:
    runner = _Runner(
        '{"number": 7, "url": "https://github.com/o/r/pull/7", "title": "t",'
        ' "state": "OPEN", "headRefName": "feature/x", "baseRefName": "main"}'
    )
    cr = ih.GitHubAdapter(runner=runner).cr_view("7")
    assert cr.host == ih.GITHUB
    assert cr.ref == "7"
    assert cr.url == "https://github.com/o/r/pull/7"
    assert cr.title == "t"
    assert cr.state == "open"
    assert cr.source_branch == "feature/x"
    assert cr.target_branch == "main"
    assert any("pr" in c and "view" in c for c in runner.calls)


def test_gitlab_cr_view_parses_mr_meta() -> None:
    runner = _Runner(
        '{"iid": 5, "web_url": "https://gitlab.com/g/r/-/merge_requests/5",'
        ' "title": "t", "state": "opened", "source_branch": "feature/x",'
        ' "target_branch": "main"}'
    )
    cr = ih.GitLabAdapter(runner=runner).cr_view("5")
    assert cr.host == ih.GITLAB
    assert cr.ref == "5"
    assert cr.url == "https://gitlab.com/g/r/-/merge_requests/5"
    assert cr.state == "open"
    assert cr.source_branch == "feature/x"
    assert cr.target_branch == "main"


def test_base_adapter_cr_view_raises_issue_host_error() -> None:
    """A backend without a cr_view implementation raises IssueHostError, which
    the best-effort packet builder degrades to the fetch-it-yourself fallback
    (deliberately not abstract — mirrors cr_checks, Story 25.1-001)."""
    runner = _Runner("{}")
    adapter = ih.GitHubAdapter(runner=runner)
    with pytest.raises(ih.IssueHostError, match="does not implement cr_view"):
        ih.IssueHostAdapter.cr_view(adapter, "7")
    # The base fallback never shells out to the host CLI.
    assert runner.calls == []


def test_github_cr_view_empty_payload_raises() -> None:
    runner = _Runner("")
    with pytest.raises(ih.IssueHostError, match="gh pr view 7 returned no change request"):
        ih.GitHubAdapter(runner=runner).cr_view("7")


def test_gitlab_cr_view_empty_payload_raises() -> None:
    runner = _Runner("")
    with pytest.raises(ih.IssueHostError, match="glab mr view 5 returned no change request"):
        ih.GitLabAdapter(runner=runner).cr_view("5")


# --- CLI verb ------------------------------------------------------------------

runner = CliRunner()


def _cli_app():
    from sdlc.cli import app

    return app


def test_cli_review_packet_prints_packet(monkeypatch) -> None:
    monkeypatch.setattr(ih, "resolve_host", lambda root, override=None: ih.GITHUB)
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None: FakeAdapter())
    result = runner.invoke(_cli_app(), ["review-packet", "7"])
    assert result.exit_code == 0
    assert "+y = 2" in result.stdout
    assert "src/app.py" in result.stdout


def test_cli_review_packet_host_error_exits_1(monkeypatch) -> None:
    def boom(root, override=None):
        raise ih.IssueHostError("no remote")

    monkeypatch.setattr(ih, "resolve_host", boom)
    result = runner.invoke(_cli_app(), ["review-packet", "7"])
    assert result.exit_code == 1
    assert "no remote" in result.output


def test_cli_review_packet_oversize_exits_3_naming_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ih, "resolve_host", lambda root, override=None: ih.GITHUB)
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None: FakeAdapter())
    result = runner.invoke(_cli_app(), ["review-packet", "7", "--max-chars", "50"])
    assert result.exit_code == 3
    assert "fall back" in result.output


# --- review prompt embedding (build-stories controller path) --------------------


def _story(story_id: str = "99.1-001"):
    from sdlc.build import Story

    return Story(
        story_id, f"Story {story_id}", "99", "demo",
        "docs/stories/epic-99.md", "P1", 1, "py", [], False,
    )


def test_build_review_prompt_embeds_packet_and_forbids_refetch() -> None:
    from sdlc.build import render_review_prompt

    block = rp.packet_block(FakeAdapter(), "7", checks="coverage_pct=93.4")
    prompt = render_review_prompt(_story(), 7, packet=block)
    assert "Review Packet" in prompt
    assert "+y = 2" in prompt
    # The packet replaces the fetch round-trips on the happy path.
    assert "instead of" in prompt
    assert "gh pr view" in prompt and "diff" in prompt
    # The distrust hardening (26.2-002) and the result contract survive.
    assert "unverified claims" in prompt
    assert "concrete named risk" in prompt
    from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

    assert RESULT_START_MARKER in prompt and RESULT_END_MARKER in prompt


def test_build_review_prompt_without_packet_is_todays_prompt() -> None:
    from sdlc.build import render_review_prompt

    prompt = render_review_prompt(_story(), 7)
    assert "Review Packet" not in prompt
    assert "unverified claims" in prompt


def test_render_stage_prompt_threads_packet_to_review_only() -> None:
    from sdlc.build import BuildOptions, _render_stage_prompt

    opts = BuildOptions(scope="epic-99")
    block = "## Review Packet\ncanary-packet\n"
    review = _render_stage_prompt("review", _story(), opts, 7, review_packet=block)
    assert "canary-packet" in review
    merge = _render_stage_prompt("merge", _story(), opts, 7, review_packet=block)
    assert "canary-packet" not in merge


# --- coverage signals ------------------------------------------------------------


def test_coverage_signals_formats_stage_result() -> None:
    from sdlc.build import _coverage_signals

    line = _coverage_signals(
        {"coverage_status": "PASS", "coverage_pct": 93.4, "tests_added": 3}
    )
    assert line is not None
    assert "coverage_status=PASS" in line
    assert "coverage_pct=93.4" in line
    assert "tests_added=3" in line


def test_coverage_signals_none_when_absent() -> None:
    from sdlc.build import _coverage_signals

    assert _coverage_signals({}) is None


# --- controller wiring: the review dispatch consumes the baked packet ------------


class _SilentLedger:
    def __init__(self):
        self.events: list[str] = []

    def stage_start(self, *a, **k):
        pass

    def stage_finish(self, *a, **k):
        pass

    def event_log(self, run_id, story_id, level, source, message):
        self.events.append(message)

    def set_story_pr(self, *a, **k):
        pass

    def set_story_status(self, *a, **k):
        pass

    def set_story_merge_sha(self, *a, **k):
        pass


def test_run_story_embeds_packet_in_review_dispatch(tmp_path, monkeypatch) -> None:
    """The review (and adversarial-slot) dispatch reuses one baked packet; the
    coverage stage's reported signals ride along."""
    from sdlc.build import BuildOptions, _run_story
    from sdlc.dispatch import AgentResult

    monkeypatch.setattr(ih, "resolve_host", lambda root, override=None: ih.GITHUB)
    monkeypatch.setattr(ih, "get_adapter", lambda host, runner=None: FakeAdapter())

    prompts: dict[str, str] = {}

    def dispatch(agent_type, prompt, story=None, **kw):
        prompts[agent_type] = prompt
        data = {
            "build": {
                "branch_name": "feature/99.1-001",
                "build_status": "SUCCESS",
                "commit_sha": "abc123",
            },
            "coverage": {
                "pr_number": 7,
                "pr_url": "https://github.com/o/r/pull/7",
                "coverage_pct": 93.4,
                "tests_added": 3,
                "coverage_status": "PASS",
            },
            "review": {
                "pr_number": 7,
                "approval_status": "APPROVED",
                "change_count": 0,
                "final_status": "APPROVED",
            },
            "merge": {
                "pr_number": 7,
                "merge_status": "MERGED",
                "merge_sha": "def456",
                "merged_at": "2026-07-15T00:00:00Z",
            },
        }[agent_type]
        return AgentResult(agent_type=agent_type, data=data, raw="")

    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    outcome = _run_story(
        _story(), opts, _SilentLedger(), "run-id", dispatch, tmp_path / "logs"
    )  # type: ignore[arg-type]

    assert outcome == "DONE"
    review_prompt = prompts["review"]
    assert "Review Packet" in review_prompt
    assert "+y = 2" in review_prompt
    assert "coverage_pct=93.4" in review_prompt


def test_run_story_review_falls_back_when_packet_unavailable(tmp_path, monkeypatch) -> None:
    """A packet miss degrades to today's fetch-it-yourself prompt and is logged."""
    from sdlc.build import BuildOptions, _run_story
    from sdlc.dispatch import AgentResult

    def boom(root, override=None):
        raise ih.IssueHostError("no remote")

    monkeypatch.setattr(ih, "resolve_host", boom)

    prompts: dict[str, str] = {}

    def dispatch(agent_type, prompt, story=None, **kw):
        prompts[agent_type] = prompt
        data = {
            "build": {
                "branch_name": "feature/99.1-001",
                "build_status": "SUCCESS",
                "commit_sha": "abc123",
            },
            "coverage": {
                "pr_number": 7,
                "pr_url": "https://github.com/o/r/pull/7",
                "coverage_pct": 93.4,
                "tests_added": 3,
                "coverage_status": "PASS",
            },
            "review": {
                "pr_number": 7,
                "approval_status": "APPROVED",
                "change_count": 0,
                "final_status": "APPROVED",
            },
            "merge": {
                "pr_number": 7,
                "merge_status": "MERGED",
                "merge_sha": "def456",
                "merged_at": "2026-07-15T00:00:00Z",
            },
        }[agent_type]
        return AgentResult(agent_type=agent_type, data=data, raw="")

    ledger = _SilentLedger()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    outcome = _run_story(_story(), opts, ledger, "run-id", dispatch, tmp_path / "logs")  # type: ignore[arg-type]

    assert outcome == "DONE"
    assert "Review Packet" not in prompts["review"]
    assert any("review packet unavailable" in e for e in ledger.events)


# --- fix-issue mirror -------------------------------------------------------------


def _fix_issue():
    from sdlc.fix_issue import FixIssue

    return FixIssue(
        number=42, title="bug", body="it breaks", state="open",
        assignees=(), labels=(),
    )


def test_fix_review_prompt_embeds_packet() -> None:
    from sdlc.fix_issue import render_review_prompt

    block = rp.packet_block(FakeAdapter(), "7")
    prompt = render_review_prompt(_fix_issue(), 7, packet=block)
    assert "Review Packet" in prompt
    assert "+y = 2" in prompt
    assert "instead of" in prompt
    assert "unverified claims" in prompt


def test_fix_review_prompt_without_packet_unchanged() -> None:
    from sdlc.fix_issue import render_review_prompt

    prompt = render_review_prompt(_fix_issue(), 7)
    assert "Review Packet" not in prompt
    assert "unverified claims" in prompt


# --- review-gate prompt markdowns (skill variants) ---------------------------------


def test_review_gate_prompt_markdowns_consume_packet_with_fallback() -> None:
    """Both skill variants consume an embedded packet and retain the gh fallback."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    for rel in (
        "plugins/autonomous-sdlc/skills/fix-issue/review-gate-prompt.md",
        "skills/fix-github-issue/review-gate-prompt.md",
    ):
        text = (repo_root / rel).read_text(encoding="utf-8")
        assert "Review Packet" in text, rel
        # Fallback path retained for when no packet is embedded.
        assert "gh pr view" in text and "gh pr diff" in text, rel
