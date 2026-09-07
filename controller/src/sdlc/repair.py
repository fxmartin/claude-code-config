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
    "MARKER_FILENAME",
    "ArtifactStatus",
    "ManagedArtifact",
    "MissingSourceError",
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "UnsafeRepairRootError",
    "WorktreeRootError",
    "apply_plan",
    "build_plan",
    "default_backup_dir",
    "default_claude_dir",
    "default_repo_root",
    "is_allowed_root",
    "is_worktree_root",
    "plan_action",
]

# Written by the installer at the real, stable checkout on a `--core` install
# (mirrored in install/core.sh). `is_allowed_root` treats its presence as proof
# that a non-$HOME root is still the authoritative install, not a scratch clone.
MARKER_FILENAME = ".sdlc-primary-root"


class WorktreeRootError(RuntimeError):
    """Raised when a repair is sourced from an ephemeral agent worktree.

    ``repair`` re-points every managed ``~/.claude`` symlink at its source
    ``repo_root``. If that root lives inside ``.claude/worktrees`` (a throwaway
    build worktree), the links dangle the moment the worktree is torn down,
    silently breaking the live install. Only the stable main checkout may own
    ``~/.claude`` — the twin of install/core.sh's ``--core`` guard (#179).
    """


class UnsafeRepairRootError(RuntimeError):
    """Raised when a repair root fails the positive allowlist (#630/#642).

    ``is_worktree_root`` only denies one known-bad shape
    (``*/.claude/worktrees/*``); any other ephemeral path — e.g. a scratch
    checkout under ``/private/tmp`` — passed it and was accepted as the source
    for every managed ``~/.claude`` symlink. ``is_allowed_root`` is a positive
    check instead: the root must sit under ``$HOME`` or carry the installer's
    marker file.
    """


class MissingSourceError(RuntimeError):
    """Raised when a planned relink's source artifact does not exist (#642).

    A repo root can pass both the worktree and allowlist checks yet still lack
    the managed sources (e.g. a bare package-install ``lib/`` directory) — in
    that case planning the relink would point a live ``~/.claude`` symlink at
    nothing while reporting success.
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


def is_allowed_root(repo_root: Path) -> bool:
    """True when *repo_root* is a plausible stable checkout.

    Positive allowlist, layered on top of ``is_worktree_root``'s deny-glob: a
    root is trusted when it sits under ``$HOME`` (where every real checkout
    lives) or carries the installer-written ``MARKER_FILENAME`` marker file.
    Anything else — e.g. a scratch clone under ``/private/tmp`` — is refused
    even though it does not match the worktree glob (#630).
    """
    resolved = repo_root.resolve()
    home = Path.home().resolve()
    if resolved == home or resolved.is_relative_to(home):
        return True
    return (repo_root / MARKER_FILENAME).is_file()

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

    Refuses (#179) when *repo_root* is an ephemeral agent worktree, and
    (#630/#642) when it fails the positive ``is_allowed_root`` allowlist or
    when a planned relink's source does not exist — each would re-point
    ``~/.claude`` at a path that vanishes, was never trustworthy, or is empty.
    """
    if is_worktree_root(repo_root):
        raise WorktreeRootError(
            f"refusing to repair from an agent worktree ({repo_root}); run "
            "sdlc repair from the main checkout so ~/.claude links to a stable "
            "path."
        )
    if not is_allowed_root(repo_root):
        raise UnsafeRepairRootError(
            f"refusing to repair from {repo_root}: not under $HOME and no "
            f"{MARKER_FILENAME} marker file found there. Run sdlc repair from "
            "the main checkout, or add the marker file if this really is the "
            "stable install root."
        )
    artifacts = tuple(
        _inspect(dest_rel, src_rel, repo_root, claude_dir)
        for dest_rel, src_rel in MANAGED_LINKS
    )
    for artifact in artifacts:
        if plan_action(artifact) is not RepairAction.NONE and not artifact.src.exists():
            raise MissingSourceError(
                f"refusing to plan {artifact.rel_dest} -> {artifact.src}: source "
                f"does not exist under the resolved repo root ({repo_root})."
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

    Defense-in-depth (#179, #630): when ``__file__`` resolves inside an
    ephemeral agent worktree, or anywhere else that fails the ``is_allowed_root``
    allowlist (e.g. a scratch checkout under ``/private/tmp``), the derived root
    is not trustworthy. Prefer the canonical install root recorded by the
    healthy marketplace link so the repair still targets the stable checkout.
    The ``build_plan`` guard is the primary protection if no healthy link is
    available to fall back to.
    """
    derived = Path(__file__).resolve().parents[3]
    if not is_worktree_root(derived) and is_allowed_root(derived):
        return derived

    marketplace = default_claude_dir() / "plugins" / "marketplaces" / "fx-claude-config"
    if marketplace.is_symlink():
        target = Path(os.readlink(marketplace))
        if not target.is_absolute():
            target = marketplace.parent / target
        canonical = target.resolve()
        if canonical.is_dir() and not is_worktree_root(canonical) and is_allowed_root(canonical):
            return canonical
    return derived


def default_claude_dir() -> Path:
    """The Claude config dir ``install.sh`` symlinks into (``~/.claude``)."""
    return Path.home() / ".claude"


def default_backup_dir(claude_dir: Path) -> Path:
    """A timestamped backup dir for displaced real files, mirroring install.sh."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return claude_dir / "backups" / f"repair-{stamp}"
