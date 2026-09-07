# ABOUTME: Tests for `sdlc repair` managed-artifact restore logic (Story 15.1-003).
# ABOUTME: Filesystem-only; no real ~/.claude or git — runs hermetically in tmp_path.

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from sdlc.repair import (
    MANAGED_LINKS,
    MARKER_FILENAME,
    ArtifactStatus,
    MissingSourceError,
    RepairAction,
    UnsafeRepairRootError,
    WorktreeRootError,
    apply_plan,
    build_plan,
    is_allowed_root,
    is_worktree_root,
)


def _is_file_artifact(src_rel: str) -> bool:
    """A managed source is a file when it carries a known file extension."""
    return src_rel.endswith((".md", ".json", ".sh"))


def _seed_repo(root: Path) -> Path:
    """Lay down a fake framework repo holding every managed source artifact.

    Also writes the ``MARKER_FILENAME`` marker, mirroring what the real
    installer leaves at the stable checkout: ``tmp_path`` is not under $HOME,
    so without it every ``build_plan`` call in this hermetic suite would trip
    the ``is_allowed_root`` guard (see the dedicated allowlist tests below,
    which build a repo WITHOUT this marker to exercise that refusal).
    """
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / MARKER_FILENAME).touch()
    for _dest_rel, src_rel in MANAGED_LINKS:
        if src_rel == ".":  # the marketplace link points at the repo root itself
            continue
        target = repo / src_rel
        if _is_file_artifact(src_rel):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {src_rel}\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / ".keep").write_text("", encoding="utf-8")
    return repo


def _link_all(repo: Path, claude_dir: Path) -> None:
    """Create a fully healthy install — every managed symlink in place."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    for dest_rel, src_rel in MANAGED_LINKS:
        src = repo / src_rel
        dest = claude_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dest)


def _status_of(plan, dest_rel: str) -> ArtifactStatus:
    for a in plan.artifacts:
        if a.rel_dest == dest_rel:
            return a.status
    raise AssertionError(f"{dest_rel} not in plan")


# --- build_plan -------------------------------------------------------------


def test_healthy_install_is_all_ok(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)

    plan = build_plan(repo, claude_dir)

    assert plan.healthy is True
    assert plan.drifted == ()
    assert all(a.status is ArtifactStatus.OK for a in plan.artifacts)
    assert len(plan.artifacts) == len(MANAGED_LINKS)


def test_missing_symlink_is_detected(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "CLAUDE.md").unlink()

    plan = build_plan(repo, claude_dir)

    assert plan.healthy is False
    assert _status_of(plan, "CLAUDE.md") is ArtifactStatus.MISSING


def test_wrong_target_symlink_is_detected(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    dest = claude_dir / "settings.json"
    dest.unlink()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    os.symlink(elsewhere, dest)

    plan = build_plan(repo, claude_dir)

    assert _status_of(plan, "settings.json") is ArtifactStatus.WRONG_TARGET


def test_broken_symlink_is_wrong_target(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    dest = claude_dir / "hooks"
    dest.unlink()
    os.symlink(tmp_path / "does-not-exist", dest)

    plan = build_plan(repo, claude_dir)

    assert _status_of(plan, "hooks") is ArtifactStatus.WRONG_TARGET


def test_real_file_in_slot_is_not_a_symlink(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    dest = claude_dir / "keybindings.json"
    dest.unlink()
    dest.write_text('{"user": "config"}', encoding="utf-8")

    plan = build_plan(repo, claude_dir)

    assert _status_of(plan, "keybindings.json") is ArtifactStatus.NOT_A_SYMLINK


def test_equivalent_target_via_symlinked_path_is_ok(tmp_path: Path) -> None:
    """A link written against a symlinked repo path is still recognized as OK."""
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # Point CLAUDE.md at the repo through a symlinked alias of the repo root.
    repo_alias = tmp_path / "repo-alias"
    os.symlink(repo, repo_alias)
    os.symlink(repo_alias / "CLAUDE.md", claude_dir / "CLAUDE.md")

    plan = build_plan(repo, claude_dir)

    assert _status_of(plan, "CLAUDE.md") is ArtifactStatus.OK


def test_relative_symlink_to_correct_source_is_ok(tmp_path: Path) -> None:
    """A relative symlink that resolves to the right source counts as OK."""
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    dest = claude_dir / "CLAUDE.md"
    rel_target = os.path.relpath(repo / "CLAUDE.md", start=dest.parent)
    os.symlink(rel_target, dest)

    plan = build_plan(repo, claude_dir)

    assert _status_of(plan, "CLAUDE.md") is ArtifactStatus.OK


def test_default_paths_resolve() -> None:
    """The default resolvers point at the real repo + ~/.claude conventions."""
    from sdlc.repair import default_backup_dir, default_claude_dir, default_repo_root

    repo_root = default_repo_root()
    assert (repo_root / "install" / "core.sh").exists()

    claude_dir = default_claude_dir()
    assert claude_dir.name == ".claude"

    backup = default_backup_dir(claude_dir)
    assert backup.parent == claude_dir / "backups"
    assert backup.name.startswith("repair-")



# --- default_repo_root: worktree fallback (#179 defense-in-depth) -----------


def test_default_repo_root_fallback_absolute_marketplace(tmp_path: Path, monkeypatch) -> None:
    """Falls back to a canonical root via an absolute marketplace symlink.

    When ``__file__`` resolves inside a worktree, ``default_repo_root`` follows
    the ``plugins/marketplaces/fx-claude-config`` symlink back to the stable
    checkout rather than returning the throwaway worktree path.
    """
    import sdlc.repair as repair_mod

    canonical = tmp_path / "stable-checkout"
    canonical.mkdir()
    (canonical / repair_mod.MARKER_FILENAME).touch()  # canonical sits outside $HOME in this test
    claude_dir = tmp_path / "dot-claude"
    marketplace_dir = claude_dir / "plugins" / "marketplaces"
    marketplace_dir.mkdir(parents=True)
    marketplace = marketplace_dir / "fx-claude-config"
    os.symlink(canonical, marketplace)

    # Treat every path except canonical as a worktree root.
    monkeypatch.setattr(repair_mod, "is_worktree_root", lambda p: p.resolve() != canonical.resolve())
    monkeypatch.setattr(repair_mod, "default_claude_dir", lambda: claude_dir)

    result = repair_mod.default_repo_root()
    assert result == canonical.resolve()


def test_default_repo_root_fallback_relative_marketplace(tmp_path: Path, monkeypatch) -> None:
    """Falls back via a relative marketplace symlink, resolving path correctly.

    Exercises the ``target = marketplace.parent / target`` branch that converts
    a relative readlink result into an absolute path before resolving.
    """
    import sdlc.repair as repair_mod

    canonical = tmp_path / "stable-checkout"
    canonical.mkdir()
    (canonical / repair_mod.MARKER_FILENAME).touch()  # canonical sits outside $HOME in this test
    claude_dir = tmp_path / "dot-claude"
    marketplace_dir = claude_dir / "plugins" / "marketplaces"
    marketplace_dir.mkdir(parents=True)
    marketplace = marketplace_dir / "fx-claude-config"
    rel_target = os.path.relpath(canonical, start=marketplace_dir)
    os.symlink(rel_target, marketplace)

    monkeypatch.setattr(repair_mod, "is_worktree_root", lambda p: p.resolve() != canonical.resolve())
    monkeypatch.setattr(repair_mod, "default_claude_dir", lambda: claude_dir)

    result = repair_mod.default_repo_root()
    assert result == canonical.resolve()


def test_default_repo_root_falls_back_to_derived_when_canonical_unsafe(
    tmp_path: Path, monkeypatch
) -> None:
    """Ignores a canonical marketplace target that fails `is_allowed_root` (#642).

    The marketplace-fallback branch guards on `is_allowed_root` in addition to
    `is_worktree_root` — a real, non-worktree canonical dir that sits outside
    $HOME with no marker file must still be refused, falling back to derived.
    """
    import sdlc.repair as repair_mod

    canonical = tmp_path / "untrusted-checkout"
    canonical.mkdir()  # no MARKER_FILENAME -- outside $HOME and unmarked
    claude_dir = tmp_path / "dot-claude"
    marketplace_dir = claude_dir / "plugins" / "marketplaces"
    marketplace_dir.mkdir(parents=True)
    marketplace = marketplace_dir / "fx-claude-config"
    os.symlink(canonical, marketplace)

    # Treat every path except canonical as a worktree root, forcing the derived
    # check to fail so the marketplace fallback is actually evaluated.
    monkeypatch.setattr(
        repair_mod, "is_worktree_root", lambda p: p.resolve() != canonical.resolve()
    )
    monkeypatch.setattr(repair_mod, "default_claude_dir", lambda: claude_dir)

    derived = Path(repair_mod.__file__).resolve().parents[3]
    result = repair_mod.default_repo_root()
    assert result == derived


def test_default_repo_root_falls_back_to_derived_when_no_marketplace(tmp_path: Path, monkeypatch) -> None:
    """Returns the derived (worktree) path when the marketplace link is absent.

    When there is no ``plugins/marketplaces/fx-claude-config`` symlink available
    to recover from, the primary ``build_plan`` guard is the only protection.
    """
    import sdlc.repair as repair_mod

    claude_dir = tmp_path / "dot-claude"
    claude_dir.mkdir()
    # No marketplace symlink created.

    monkeypatch.setattr(repair_mod, "is_worktree_root", lambda p: True)
    monkeypatch.setattr(repair_mod, "default_claude_dir", lambda: claude_dir)

    derived = Path(repair_mod.__file__).resolve().parents[3]
    result = repair_mod.default_repo_root()
    assert result == derived


# --- apply_plan: restore ----------------------------------------------------


def test_apply_restores_missing_link(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "agents").unlink()

    plan = build_plan(repo, claude_dir)
    results = apply_plan(plan, dry_run=False, backup_dir=tmp_path / "bk")

    dest = claude_dir / "agents"
    assert dest.is_symlink()
    assert Path(os.readlink(dest)).resolve() == (repo / "agents").resolve()
    acted = [r for r in results if r.action is not RepairAction.NONE]
    assert [r.action for r in acted] == [RepairAction.LINKED]


def test_apply_relinks_wrong_target(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    dest = claude_dir / "docs"
    dest.unlink()
    os.symlink(tmp_path / "stray", dest)

    plan = build_plan(repo, claude_dir)
    apply_plan(plan, dry_run=False, backup_dir=tmp_path / "bk")

    assert Path(os.readlink(dest)).resolve() == (repo / "docs").resolve()


def test_apply_backs_up_real_file_then_links(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    dest = claude_dir / "settings.json"
    dest.unlink()
    dest.write_text("USER DATA", encoding="utf-8")
    backup_dir = tmp_path / "bk"

    plan = build_plan(repo, claude_dir)
    results = apply_plan(plan, dry_run=False, backup_dir=backup_dir)

    # The slot is now the managed symlink…
    assert dest.is_symlink()
    assert Path(os.readlink(dest)).resolve() == (repo / "settings.json").resolve()
    # …and the user's real file was preserved (not destroyed) in the backup dir.
    backed = [r for r in results if r.action is RepairAction.BACKED_UP][0]
    assert backed.backup_path is not None
    assert backed.backup_path.read_text(encoding="utf-8") == "USER DATA"


def test_apply_creates_nested_marketplace_parent(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    # Nothing linked at all — the nested plugins/marketplaces parents are absent.
    claude_dir.mkdir()

    plan = build_plan(repo, claude_dir)
    apply_plan(plan, dry_run=False, backup_dir=tmp_path / "bk")

    market = claude_dir / "plugins" / "marketplaces" / "fx-claude-config"
    assert market.is_symlink()
    assert Path(os.readlink(market)).resolve() == repo.resolve()


def test_apply_is_idempotent(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    # First repair from a bare dir restores everything…
    apply_plan(build_plan(repo, claude_dir), dry_run=False, backup_dir=tmp_path / "bk")
    # …a second pass is a clean no-op.
    plan2 = build_plan(repo, claude_dir)
    assert plan2.healthy is True
    results2 = apply_plan(plan2, dry_run=False, backup_dir=tmp_path / "bk")
    assert all(r.action is RepairAction.NONE for r in results2)


# --- safety: dry-run + unmanaged files --------------------------------------


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "skills").unlink()

    plan = build_plan(repo, claude_dir)
    results = apply_plan(plan, dry_run=True, backup_dir=tmp_path / "bk")

    # Reported as a would-be restore…
    assert any(r.action is RepairAction.LINKED for r in results)
    # …but the filesystem is untouched.
    assert not (claude_dir / "skills").exists()
    assert _status_of(build_plan(repo, claude_dir), "skills") is ArtifactStatus.MISSING


def test_repair_never_touches_unmanaged_files(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "skills").unlink()  # force at least one repair action

    # User-owned artifacts that are NOT in the managed set.
    user_file = claude_dir / "my-notes.md"
    user_file.write_text("private", encoding="utf-8")
    user_dir = claude_dir / "projects"
    user_dir.mkdir()
    (user_dir / "data.db").write_text("ledger", encoding="utf-8")

    apply_plan(build_plan(repo, claude_dir), dry_run=False, backup_dir=tmp_path / "bk")

    assert user_file.read_text(encoding="utf-8") == "private"
    assert (user_dir / "data.db").read_text(encoding="utf-8") == "ledger"


# --- worktree-root guard (#179) ---------------------------------------------


def test_is_worktree_root_detects_agent_worktree(tmp_path: Path) -> None:
    wt = tmp_path / ".claude" / "worktrees" / "agent-run-story" / "controller"
    assert is_worktree_root(wt) is True


def test_is_worktree_root_accepts_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "claude-code-config" / "controller"
    assert is_worktree_root(main) is False


def test_build_plan_refuses_worktree_root(tmp_path: Path) -> None:
    """build_plan must refuse a worktree source before any plan exists to apply.

    Guards direct callers of build_plan/apply_plan too, not just the CLI: a
    repair sourced from an ephemeral worktree would re-point ~/.claude into a
    path that vanishes on teardown.
    """
    wt = tmp_path / ".claude" / "worktrees" / "agent-test-1"
    repo = _seed_repo(wt)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)

    with pytest.raises(WorktreeRootError, match="worktree"):
        build_plan(repo, claude_dir)


# --- positive allowlist guard (#630/#642) ------------------------------------


def test_is_allowed_root_accepts_home_rooted_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "checkout"
    repo.mkdir()

    assert is_allowed_root(repo) is True


def test_is_allowed_root_accepts_marker_file_outside_home(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / MARKER_FILENAME).touch()

    assert is_allowed_root(repo) is True


def test_is_allowed_root_refuses_non_home_root_without_marker(tmp_path: Path) -> None:
    """A scratch checkout outside $HOME with no marker is refused (#630).

    This is the exact incident shape: /private/tmp/branch-check-31002 is not
    a worktree-glob match, so ``is_worktree_root`` alone let it through.
    """
    repo = tmp_path / "private" / "tmp" / "branch-check-31002"
    repo.mkdir(parents=True)

    assert is_worktree_root(repo) is False  # the old guard alone misses this
    assert is_allowed_root(repo) is False


def test_build_plan_refuses_unsafe_root_that_is_not_a_worktree(tmp_path: Path) -> None:
    """build_plan refuses a non-$HOME, non-marker root even without a worktree glob hit."""
    repo = _seed_repo(tmp_path)
    (repo / MARKER_FILENAME).unlink()  # simulate a root outside $HOME, un-marked
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    with pytest.raises(UnsafeRepairRootError, match=r"\$HOME"):
        build_plan(repo, claude_dir)


def test_build_plan_accepts_marker_bearing_root_outside_worktree_glob(tmp_path: Path) -> None:
    """A marker-bearing root outside the worktree glob is accepted, not refused."""
    repo = _seed_repo(tmp_path)  # _seed_repo writes MARKER_FILENAME
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    plan = build_plan(repo, claude_dir)

    assert len(plan.artifacts) == len(MANAGED_LINKS)


def test_build_plan_refuses_missing_source(tmp_path: Path) -> None:
    """build_plan must refuse to plan a relink whose source does not exist (#642).

    Reproduces the second half of the incident: a resolved root that passes
    both the worktree and allowlist checks but is missing the actual managed
    sources (e.g. a bare package-install ``lib/`` dir) must not silently plan
    a relink into a void.
    """
    repo = _seed_repo(tmp_path)
    shutil.rmtree(repo / "hooks")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    with pytest.raises(MissingSourceError, match="hooks"):
        build_plan(repo, claude_dir)


# --- managed set authority: parity with install/core.sh ---------------------


def test_managed_links_match_install_core_sh() -> None:
    """The managed set must mirror install/core.sh's install_core_run() exactly.

    `repair` is a thin wrapper over the artifacts `install.sh` creates; if the
    installer gains/loses a symlink this guard fails until MANAGED_LINKS follows.
    """
    repo_root = Path(__file__).resolve().parents[2]
    core_sh = (repo_root / "install" / "core.sh").read_text(encoding="utf-8")

    # Restrict to the install_core_run() body so uninstall's remove_symlink and
    # the ensure_dir lines do not leak into the comparison.
    run_body = core_sh.split("install_core_run()", 1)[1].split("install_core_uninstall()", 1)[0]

    pairs: set[tuple[str, str]] = set()
    for m in re.finditer(
        r'create_symlink\s+"\$SCRIPT_DIR(?:/([^"]+))?"\s+"\$CLAUDE_DIR/([^"]+)"',
        run_body,
    ):
        src_rel = m.group(1) or "."
        dest_rel = m.group(2)
        pairs.add((dest_rel, src_rel))

    assert pairs == set(MANAGED_LINKS)


def test_install_core_sh_marker_matches_repair_py() -> None:
    """install/core.sh must write the same marker filename repair.py checks (#642).

    is_allowed_root's marker branch is only reachable in practice if the
    installer actually writes MARKER_FILENAME somewhere; this locks
    install/core.sh's literal and write call in place so the two never
    silently diverge, mirroring test_managed_links_match_install_core_sh above.
    """
    repo_root = Path(__file__).resolve().parents[2]
    core_sh = (repo_root / "install" / "core.sh").read_text(encoding="utf-8")

    match = re.search(r'SDLC_PRIMARY_ROOT_MARKER="([^"]+)"', core_sh)
    assert match is not None, "install/core.sh must define SDLC_PRIMARY_ROOT_MARKER"
    assert match.group(1) == MARKER_FILENAME

    run_body = core_sh.split("install_core_run()", 1)[1].split("install_core_uninstall()", 1)[0]
    assert "$SDLC_PRIMARY_ROOT_MARKER" in run_body, (
        "install_core_run() must write the primary-root marker so a non-$HOME "
        "install can later be trusted by sdlc repair"
    )
