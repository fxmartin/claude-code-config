# ABOUTME: Tests for `sdlc repair` managed-artifact restore logic (Story 15.1-003).
# ABOUTME: Filesystem-only; no real ~/.claude or git — runs hermetically in tmp_path.

from __future__ import annotations

import os
import re
from pathlib import Path

from sdlc.repair import (
    MANAGED_LINKS,
    ArtifactStatus,
    RepairAction,
    apply_plan,
    build_plan,
)


def _is_file_artifact(src_rel: str) -> bool:
    """A managed source is a file when it carries a known file extension."""
    return src_rel.endswith((".md", ".json", ".sh"))


def _seed_repo(root: Path) -> Path:
    """Lay down a fake framework repo holding every managed source artifact."""
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
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
