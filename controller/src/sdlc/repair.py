# ABOUTME: `sdlc repair` logic — restore the framework's managed symlinks/config
# ABOUTME: (Story 15.1-003). A thin, idempotent wrapper over install/core.sh's set.

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = [
    "MANAGED_LINKS",
    "ArtifactStatus",
    "ManagedArtifact",
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "WorktreeRootError",
    "apply_plan",
    "build_plan",
    "default_backup_dir",
    "default_claude_dir",
    "default_repo_root",
    "is_worktree_root",
    "plan_action",
]


class WorktreeRootError(RuntimeError):
    """Raised when a repair is sourced from an ephemeral agent worktree.

    ``repair`` re-points every managed ``~/.claude`` symlink at its source
    ``repo_root``. If that root lives inside ``.claude/worktrees`` (a throwaway
    build worktree), the links dangle the moment the worktree is torn down,
    silently breaking the live install. Only the stable main checkout may own
    ``~/.claude`` — the twin of install/core.sh's ``--core`` guard (#179).
    """


def is_worktree_root(repo_root: Path) -> bool:
    """True when *repo_root* lives inside an ephemeral agent worktree.

    Mirrors install/core.sh's glob guard (``*/.claude/worktrees/*``): a path
    with ``.claude/worktrees`` followed by at least one more component is a
    throwaway build worktree, never the stable main checkout.
    """
    parts = repo_root.resolve().parts
    # range(len - 2) guarantees a component exists *after* "worktrees", matching
    # the trailing "/*" in the shell glob.
    return any(
        parts[i] == ".claude" and parts[i + 1] == "worktrees"
        for i in range(len(parts) - 2)
    )

# The managed-artifact set, mirroring install/core.sh's install_core_run().
# Each entry is (destination relative to the Claude config dir, source relative
# to the repo root). `repair` restores exactly these symlinks and nothing else —
# anything outside this set is never touched (no destructive action on user
# files). A "." source is the repo root itself (the plugin marketplace link).
# test_repair.py::test_managed_links_match_install_core_sh guards parity with
# the installer so the two never silently diverge.
MANAGED_LINKS: tuple[tuple[str, str], ...] = (
    ("CLAUDE.md", "CLAUDE.md"),
    ("agents", "agents"),
    ("commands", "commands"),
    ("settings.json", "settings.json"),
    ("statusline-command.sh", "statusline-command.sh"),
    ("keybindings.json", "keybindings.json"),
    ("reference-docs", "reference-docs"),
    ("docs", "docs"),
    ("skills", "skills"),
    ("hooks", "hooks"),
    ("plugins/marketplaces/fx-claude-config", "."),
)


class ArtifactStatus(enum.Enum):
    """Health verdict for one managed destination under the Claude config dir."""

    OK = "ok"  # a symlink pointing at the correct repo source
    MISSING = "missing"  # nothing at the destination
    WRONG_TARGET = "wrong_target"  # a symlink pointing elsewhere (incl. broken)
    NOT_A_SYMLINK = "not_a_symlink"  # a real file/dir occupies the slot


class RepairAction(enum.Enum):
    """What `apply_plan` does (or would do, in dry-run) for one artifact."""

    NONE = "none"  # already healthy — nothing to do
    LINKED = "linked"  # created a missing symlink
    RELINKED = "relinked"  # replaced a wrong-target symlink
    BACKED_UP = "backed_up"  # moved a real file/dir aside, then linked


# Status → the action that restores it. OK needs nothing; the rest each map to a
# single deterministic remedy.
_ACTION_FOR_STATUS: dict[ArtifactStatus, RepairAction] = {
    ArtifactStatus.OK: RepairAction.NONE,
    ArtifactStatus.MISSING: RepairAction.LINKED,
    ArtifactStatus.WRONG_TARGET: RepairAction.RELINKED,
    ArtifactStatus.NOT_A_SYMLINK: RepairAction.BACKED_UP,
}


@dataclass(frozen=True)
class ManagedArtifact:
    """One managed symlink: where it should point and its current health."""

    rel_dest: str  # path relative to the Claude config dir
    src: Path  # absolute source in the repo
    dest: Path  # absolute destination under the Claude config dir
    status: ArtifactStatus

    @property
    def healthy(self) -> bool:
        return self.status is ArtifactStatus.OK


@dataclass(frozen=True)
class RepairPlan:
    """The health of every managed artifact for a (repo, claude_dir) pair."""

    artifacts: tuple[ManagedArtifact, ...]

    @property
    def healthy(self) -> bool:
        """True only when every managed symlink is in place and correct."""
        return all(a.healthy for a in self.artifacts)

    @property
    def drifted(self) -> tuple[ManagedArtifact, ...]:
        return tuple(a for a in self.artifacts if not a.healthy)


@dataclass(frozen=True)
class RepairResult:
    """The outcome of restoring one artifact (or the no-op when healthy)."""

    artifact: ManagedArtifact
    action: RepairAction
    backup_path: Path | None = None


def _inspect(rel_dest: str, src_rel: str, repo_root: Path, claude_dir: Path) -> ManagedArtifact:
    """Classify the current state of one managed destination.

    The target comparison resolves symlinks on both sides, so a link written
    against a symlinked alias of the repo (e.g. ``/var`` vs ``/private/var`` on
    macOS) is still recognized as ``OK`` rather than flagged as false drift.
    """
    src = repo_root / src_rel  # ``repo_root / "."`` collapses to repo_root
    dest = claude_dir / rel_dest

    if dest.is_symlink():
        target = Path(os.readlink(dest))
        if not target.is_absolute():
            target = dest.parent / target
        status = (
            ArtifactStatus.OK
            if target.resolve() == src.resolve()
            else ArtifactStatus.WRONG_TARGET
        )
    elif dest.exists():
        status = ArtifactStatus.NOT_A_SYMLINK
    else:
        status = ArtifactStatus.MISSING

    return ManagedArtifact(rel_dest=rel_dest, src=src, dest=dest, status=status)


def build_plan(repo_root: Path, claude_dir: Path) -> RepairPlan:
    """Inspect every managed artifact and return the resulting repair plan.

    Refuses (#179) when *repo_root* is an ephemeral agent worktree: building a
    plan there would re-point ``~/.claude`` at a path that vanishes on teardown.
    Guarding here protects both ``sdlc repair`` and any direct ``apply_plan``
    caller, since no plan is produced for a worktree source.
    """
    if is_worktree_root(repo_root):
        raise WorktreeRootError(
            f"refusing to repair from an agent worktree ({repo_root}); run "
            "sdlc repair from the main checkout so ~/.claude links to a stable "
            "path."
        )
    artifacts = tuple(
        _inspect(dest_rel, src_rel, repo_root, claude_dir)
        for dest_rel, src_rel in MANAGED_LINKS
    )
    return RepairPlan(artifacts=artifacts)


def plan_action(artifact: ManagedArtifact) -> RepairAction:
    """The single deterministic action that restores *artifact*."""
    return _ACTION_FOR_STATUS[artifact.status]


def _perform(artifact: ManagedArtifact, action: RepairAction, backup_path: Path | None) -> None:
    dest = artifact.dest
    if action is RepairAction.RELINKED:
        dest.unlink()  # drop the wrong (possibly broken) symlink
    elif action is RepairAction.BACKED_UP:
        assert backup_path is not None  # set by apply_plan for this action
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        os.rename(dest, backup_path)  # preserve the user's real file/dir
    dest.parent.mkdir(parents=True, exist_ok=True)  # nested marketplace path
    os.symlink(artifact.src, dest)


def apply_plan(
    plan: RepairPlan,
    *,
    dry_run: bool,
    backup_dir: Path,
) -> list[RepairResult]:
    """Restore every drifted artifact idempotently; return per-artifact outcomes.

    Healthy artifacts are no-ops. A real file/dir occupying a managed slot is
    *moved* into *backup_dir* (never deleted) before the symlink is created, so
    the operation is recoverable. In ``dry_run`` mode nothing is written — each
    result still reports the action that *would* run.
    """
    results: list[RepairResult] = []
    for artifact in plan.artifacts:
        action = plan_action(artifact)
        if action is RepairAction.NONE:
            results.append(RepairResult(artifact=artifact, action=action))
            continue

        backup_path = (
            backup_dir / artifact.dest.name
            if action is RepairAction.BACKED_UP
            else None
        )
        if not dry_run:
            _perform(artifact, action, backup_path)
        results.append(
            RepairResult(artifact=artifact, action=action, backup_path=backup_path)
        )
    return results


def default_repo_root() -> Path:
    """The framework repo root that owns the managed artifacts.

    ``controller/src/sdlc/repair.py`` → ``parents[3]`` is the repo root where
    ``install.sh``, ``CLAUDE.md`` and the rest of the managed set live.

    Defense-in-depth (#179): when ``__file__`` resolves inside an ephemeral
    agent worktree (``uv run sdlc repair`` invoked from a worktree cwd loads the
    worktree's own copy of this module), the derived root is the throwaway path.
    Prefer the canonical install root recorded by the healthy marketplace link
    so the repair still targets the stable checkout. The ``build_plan`` guard is
    the primary protection if no healthy link is available to fall back to.
    """
    derived = Path(__file__).resolve().parents[3]
    if not is_worktree_root(derived):
        return derived

    marketplace = default_claude_dir() / "plugins" / "marketplaces" / "fx-claude-config"
    if marketplace.is_symlink():
        target = Path(os.readlink(marketplace))
        if not target.is_absolute():
            target = marketplace.parent / target
        canonical = target.resolve()
        if canonical.is_dir() and not is_worktree_root(canonical):
            return canonical
    return derived


def default_claude_dir() -> Path:
    """The Claude config dir ``install.sh`` symlinks into (``~/.claude``)."""
    return Path.home() / ".claude"


def default_backup_dir(claude_dir: Path) -> Path:
    """A timestamped backup dir for displaced real files, mirroring install.sh."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return claude_dir / "backups" / f"repair-{stamp}"
