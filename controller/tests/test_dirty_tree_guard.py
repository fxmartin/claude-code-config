# ABOUTME: Tests for the dirty-shared-checkout guard + stash surfacing (issue #590).
# ABOUTME: Real temp git repos for the probes; injected seams for the orchestration.

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    BuildResult,
    Ledger,
    dirty_tree_paths,
    format_dirty_tree,
    list_stashes,
    render_build_prompt,
    run_build,
)
from sdlc.clean import plan_clean, run_clean
from sdlc.doctor import check_stashes, run_doctor
from sdlc.fix_issue import FixBatchOptions, FixOptions, parse_fix_args, run_fix, run_fix_batch
from sdlc.fix_issue import render_build_prompt as render_fix_build_prompt
from sdlc.registry import Registry

from test_fix_issue import (  # noqa: E402 — sibling test module, reuses its fakes
    FakeBatchGh,
    FakeGh,
    RecordingDispatcher,
    _batch_issue,
    _issue_json,
)


# --- git fixture helpers ----------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A repo on ``main`` with one committed file. No remote, no hooks."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "CLAUDE.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def _story(story_id: str = "99.1-001"):
    from sdlc.build import Story

    return Story(
        story_id, f"Story {story_id}", story_id.split(".", 1)[0].zfill(2), "x",
        "epic-x.md", "P1", 1, "py", [], False,
    )


# ---------------------------------------------------------------------------
# The probe: what counts as a dirty shared checkout
# ---------------------------------------------------------------------------


def test_dirty_tree_paths_is_empty_on_a_clean_repo(tmp_path) -> None:
    assert dirty_tree_paths(_init_repo(tmp_path)) == []


def test_dirty_tree_paths_reports_an_uncommitted_tracked_edit(tmp_path) -> None:
    """The #590 scenario: hand-edited, unpushed content in a tracked file."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("base\nCI Compatibility\n", encoding="utf-8")
    assert dirty_tree_paths(root) == ["CLAUDE.md"]


def test_dirty_tree_paths_reports_a_staged_edit(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    assert dirty_tree_paths(root) == ["CLAUDE.md"]


def test_dirty_tree_paths_ignores_untracked_scratch_files(tmp_path) -> None:
    """Untracked files neither block `checkout -b` nor land in a bare stash."""
    root = _init_repo(tmp_path)
    (root / "scratch.tmp").write_text("junk\n", encoding="utf-8")
    assert dirty_tree_paths(root) == []


def test_dirty_tree_paths_degrades_to_empty_outside_a_repo(tmp_path) -> None:
    """An un-inspectable tree must not block every run."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert dirty_tree_paths(plain) == []


def test_dirty_tree_paths_reports_every_changed_tracked_file(tmp_path) -> None:
    root = _init_repo(tmp_path)
    for name in ("a.md", "b.md"):
        (root / name).write_text("x\n", encoding="utf-8")
    _git(root, "add", "a.md", "b.md")
    _git(root, "commit", "-q", "-m", "chore: more")
    (root / "a.md").write_text("edited\n", encoding="utf-8")
    (root / "b.md").write_text("edited\n", encoding="utf-8")
    assert sorted(dirty_tree_paths(root)) == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# The refusal message
# ---------------------------------------------------------------------------


def test_format_dirty_tree_names_the_paths_and_the_opt_out(tmp_path) -> None:
    msg = format_dirty_tree(tmp_path, ["CLAUDE.md", "settings.json"], "sdlc fix")
    assert "DIRTY_WORKING_TREE" in msg
    assert "CLAUDE.md" in msg and "settings.json" in msg
    assert "sdlc fix --allow-dirty" in msg
    # It must explain *why*, or the refusal reads as a pointless obstruction.
    assert "stash" in msg and "#590" in msg


def test_format_dirty_tree_truncates_a_long_list(tmp_path) -> None:
    paths = [f"f{i}.py" for i in range(25)]
    msg = format_dirty_tree(tmp_path, paths, "sdlc build")
    assert "25 uncommitted change(s)" in msg
    assert "(+15 more)" in msg
    assert "f24.py" not in msg


# ---------------------------------------------------------------------------
# Regression: a run must refuse a dirty shared checkout before any dispatch
# ---------------------------------------------------------------------------


def test_run_fix_refuses_to_start_on_a_dirty_checkout(tmp_path) -> None:
    """Issue #590: the run that would have let an agent stash FX's work never starts."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")
    dispatch = RecordingDispatcher()

    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )

    assert result.status == "ABORTED"
    assert result.dirty_tree == ["CLAUDE.md"]
    # Nothing dispatched, so nothing could have stashed anything...
    assert dispatch.agents() == []
    # ...and no run row exists to be left half-open.
    assert result.run_id is None
    # The user's work is untouched and still visible to `git status`.
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "hand-edited\n"
    assert list_stashes(root) == []


def test_run_fix_uses_the_real_probe_on_a_real_run(tmp_path) -> None:
    """The default seam — not just an injected one — refuses a dirty checkout."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")

    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        # dispatcher=None marks a real run; the guard returns before preflight
        # and before the first dispatch, so no agent is ever launched.
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=root,
        registry=Registry(tmp_path / "registry.json"),
    )

    assert result.status == "ABORTED"
    assert result.dirty_tree == ["CLAUDE.md"]


def test_run_fix_allow_dirty_opts_out(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")
    dispatch = RecordingDispatcher()

    result = run_fix(
        FixOptions(issue=1, allow_dirty=True),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )

    assert result.status == "DONE"
    assert result.dirty_tree == []
    assert "build" in dispatch.agents()


def test_run_fix_starts_normally_on_a_clean_checkout(tmp_path) -> None:
    root = _init_repo(tmp_path)
    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )
    assert result.status == "DONE"


def test_run_fix_does_not_probe_git_when_a_dispatcher_is_injected(
    tmp_path, monkeypatch
) -> None:
    """A fake-dispatch run launches no agent, so it must not pay for (or trip on)
    the probe — this is what keeps every existing orchestration test unchanged."""
    import sdlc.fix_issue as fix_mod

    def _explode(_root):  # pragma: no cover - must never be reached
        raise AssertionError("the git probe must not run for an injected dispatcher")

    monkeypatch.setattr(fix_mod, "dirty_tree_paths", _explode)
    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=tmp_path,
    )
    assert result.status == "DONE"


def test_run_fix_batch_refuses_to_start_on_a_dirty_checkout(tmp_path) -> None:
    """Every issue in a batch shares one checkout, so one refusal covers them all."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")
    dispatch = RecordingDispatcher()

    result = run_fix_batch(
        FixBatchOptions(target="all"),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1)]),
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )

    assert result.status == "ABORTED"
    assert result.dirty_tree == ["CLAUDE.md"]
    assert result.run_id is None
    assert dispatch.agents() == []


def test_run_build_refuses_to_start_on_a_dirty_checkout(tmp_path) -> None:
    """`sdlc build` shares the hazard: its build agent cuts branches in the root."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")
    dispatch = RecordingDispatcher()

    result = run_build(
        BuildOptions(scope="all"),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )

    assert isinstance(result, BuildResult)
    assert result.dirty_tree == ["CLAUDE.md"]
    assert result.run_id is None
    assert dispatch.agents() == []


def test_run_build_allow_dirty_opts_out(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")

    result = run_build(
        BuildOptions(scope="all", allow_dirty=True),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )
    assert result.dirty_tree == []
    assert result.run_id is not None


def test_run_build_binds_the_real_probe_on_a_real_run(tmp_path, monkeypatch) -> None:
    """The default seam is the real git probe, rooted at the run's project root.

    Asserted through a spy rather than by letting ``dispatcher=None`` reach the
    pipeline: a regression here must fail the test, not launch real agents.
    """
    import sdlc.build as build_mod

    seen: list[Path] = []

    def _spy(root: Path) -> list[str]:
        seen.append(root)
        return ["CLAUDE.md"]

    monkeypatch.setattr(build_mod, "dirty_tree_paths", _spy)
    root = _init_repo(tmp_path)

    result = run_build(
        BuildOptions(scope="all"),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        preflight=lambda: True,  # also disarms the recursion guard
        root=root,
    )

    assert seen == [root]
    assert result.dirty_tree == ["CLAUDE.md"]
    assert result.run_id is None


def test_run_fix_batch_binds_the_real_probe_on_a_real_run(tmp_path, monkeypatch) -> None:
    """Same default binding for the batch path, asserted the same safe way."""
    import sdlc.fix_issue as fix_mod

    seen: list[Path] = []

    def _spy(root: Path) -> list[str]:
        seen.append(root)
        return ["CLAUDE.md"]

    monkeypatch.setattr(fix_mod, "dirty_tree_paths", _spy)
    root = _init_repo(tmp_path)

    result = run_fix_batch(
        FixBatchOptions(target="all"),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1)]),
        root=root,
        registry=Registry(tmp_path / "registry.json"),
    )

    assert seen == [root]
    assert result.dirty_tree == ["CLAUDE.md"]
    assert result.run_id is None


def test_dry_run_is_unaffected_by_a_dirty_checkout(tmp_path) -> None:
    """A dry run dispatches nothing, so it has no checkout to protect."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("hand-edited\n", encoding="utf-8")

    result = run_build(
        BuildOptions(scope="all", dry_run=True),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        root=root,
        dirty_check=lambda: dirty_tree_paths(root),
    )
    assert result.dry_run is True
    assert result.dirty_tree == []


# ---------------------------------------------------------------------------
# The flag surface
# ---------------------------------------------------------------------------


def test_parse_fix_args_accepts_allow_dirty() -> None:
    opts = parse_fix_args(["590", "--allow-dirty"])
    assert isinstance(opts, FixOptions)
    assert opts.allow_dirty is True


def test_parse_fix_args_defaults_allow_dirty_off() -> None:
    opts = parse_fix_args(["590"])
    assert isinstance(opts, FixOptions)
    assert opts.allow_dirty is False


def test_parse_fix_args_accepts_allow_dirty_on_a_batch() -> None:
    opts = parse_fix_args(["all", "--allow-dirty"])
    assert isinstance(opts, FixBatchOptions)
    assert opts.allow_dirty is True


def test_parse_build_args_accepts_allow_dirty() -> None:
    from sdlc.build import parse_build_args

    assert parse_build_args(["all", "--allow-dirty"]).allow_dirty is True
    assert parse_build_args(["all"]).allow_dirty is False


# ---------------------------------------------------------------------------
# Regression: the build prompts must forbid the stash workaround
# ---------------------------------------------------------------------------


def _issue(number: int = 590):
    from sdlc.fix_issue import FixIssue

    return FixIssue(
        number=number, title="Crashed run strands work", body="boom",
        state="OPEN", labels=[], assignees=[],
    )


def test_fix_build_prompt_forbids_stashing_around_a_failed_checkout() -> None:
    """Issue #590: 'fail immediately' alone let the agent improvise `git stash`."""
    prompt = render_fix_build_prompt(_issue(), {"root_cause": "x"}, FixOptions(issue=590))
    lowered = prompt.lower()
    assert "git stash" in lowered
    assert "never run `git stash`" in lowered


def test_build_prompt_forbids_stashing_around_a_failed_checkout() -> None:
    prompt = render_build_prompt(_story("99.1-001"), BuildOptions())
    lowered = prompt.lower()
    assert "git stash" in lowered
    assert "never run `git stash`" in lowered


# ---------------------------------------------------------------------------
# Surfacing an already-orphaned stash: `sdlc doctor`
# ---------------------------------------------------------------------------


def _stash(root: Path, message: str, path: str = "CLAUDE.md") -> None:
    (root / path).write_text(f"{message}\n", encoding="utf-8")
    _git(root, "stash", "push", "-q", "-m", message)


def test_check_stashes_is_clean_when_the_stack_is_empty(tmp_path) -> None:
    finding = check_stashes(_init_repo(tmp_path))
    assert finding.status == "CLEAN"
    assert finding.remedy == ""


def test_check_stashes_warns_and_names_the_recovery_command(tmp_path) -> None:
    """The #590 hazard: work `git status` cannot see, with nothing pointing at it."""
    root = _init_repo(tmp_path)
    _stash(root, "wip-before-586")

    finding = check_stashes(root)

    assert finding.status == "WARN"
    assert "wip-before-586" in finding.detail
    assert "stash@{0}" in finding.detail
    assert "git stash apply stash@{0}" in finding.remedy


def test_check_stashes_reports_bare_wip_entries(tmp_path) -> None:
    """The real orphans were anonymous `WIP on <branch>` entries, not labelled ones."""
    root = _init_repo(tmp_path)
    (root / "CLAUDE.md").write_text("crashed run's work\n", encoding="utf-8")
    _git(root, "stash", "push", "-q")  # no -m: git's default "WIP on main: ..."

    finding = check_stashes(root)

    assert finding.status == "WARN"
    assert "WIP on main" in finding.detail


def test_check_stashes_truncates_a_deep_stack(tmp_path) -> None:
    root = _init_repo(tmp_path)
    for i in range(8):
        _stash(root, f"wip-{i}")
    finding = check_stashes(root)
    assert finding.status == "WARN"
    assert "8 stash entries" in finding.detail
    assert "+3 more" in finding.detail


def test_check_stashes_is_clean_outside_a_repo(tmp_path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert check_stashes(plain).status == "CLEAN"


def test_run_doctor_includes_the_stash_check(tmp_path) -> None:
    root = _init_repo(tmp_path)
    _stash(root, "wip-before-586")

    report = run_doctor(
        repo_root=root,
        claude_dir=tmp_path / "missing-claude",
        db_path=tmp_path / ".sdlc-state.db",
        registry=Registry(tmp_path / "registry.json"),
        dep_probe=lambda _tool: True,
    )

    stash_findings = [f for f in report.findings if f.check == "stash"]
    assert len(stash_findings) == 1
    assert stash_findings[0].status == "WARN"


# ---------------------------------------------------------------------------
# Surfacing an already-orphaned stash: `sdlc clean`
# ---------------------------------------------------------------------------


def test_clean_reports_a_leftover_stash(tmp_path) -> None:
    root = _init_repo(tmp_path)
    _stash(root, "wip-before-586")

    plan = plan_clean(
        root=root,
        db_path=tmp_path / ".sdlc-state.db",
        registry=Registry(tmp_path / "registry.json"),
        gh_merged_fn=lambda _b, _r: False,
    )

    assert [item.name for item in plan.stashes] == ["stash@{0}"]
    assert "git stash apply stash@{0}" in plan.stashes[0].reason


def test_clean_never_makes_a_stash_a_removal_candidate(tmp_path) -> None:
    """Dropping a stash would destroy the un-backed-up work this exists to surface."""
    root = _init_repo(tmp_path)
    _stash(root, "wip-before-586")

    plan = run_clean(
        root=root,
        db_path=tmp_path / ".sdlc-state.db",
        registry=Registry(tmp_path / "registry.json"),
        gh_merged_fn=lambda _b, _r: False,
        force=True,
    )

    assert not any(item.kind == "stash" for item in plan.candidates)
    # --force ran and the stash is still recoverable.
    assert plan.forced is True
    assert [ref for ref, _subject in list_stashes(root)] == ["stash@{0}"]


def test_clean_reports_nothing_when_there_is_no_stash(tmp_path) -> None:
    root = _init_repo(tmp_path)
    plan = plan_clean(
        root=root,
        db_path=tmp_path / ".sdlc-state.db",
        registry=Registry(tmp_path / "registry.json"),
        gh_merged_fn=lambda _b, _r: False,
    )
    assert plan.stashes == []


# ---------------------------------------------------------------------------
# list_stashes: the shared probe
# ---------------------------------------------------------------------------


def test_list_stashes_is_empty_on_a_fresh_repo(tmp_path) -> None:
    assert list_stashes(_init_repo(tmp_path)) == []


def test_list_stashes_is_newest_first_with_refs_and_subjects(tmp_path) -> None:
    root = _init_repo(tmp_path)
    _stash(root, "older")
    _stash(root, "newer")

    entries = list_stashes(root)

    assert [ref for ref, _ in entries] == ["stash@{0}", "stash@{1}"]
    assert "newer" in entries[0][1]
    assert "older" in entries[1][1]


def test_list_stashes_degrades_to_empty_outside_a_repo(tmp_path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert list_stashes(plain) == []
