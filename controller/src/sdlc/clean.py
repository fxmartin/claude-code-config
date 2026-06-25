# ABOUTME: `sdlc clean` — safe workspace garbage collection (Story 15.3-001).
# ABOUTME: Dry-run by default; registry/pid-aware so it is safe beside a live build.

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdlc.build import Ledger, _git, remove_story_worktree
from sdlc.ledger_view import default_db_path
from sdlc.registry import Registry, pid_alive

__all__ = ["CleanItem", "CleanPlan", "plan_clean", "run_clean"]

# Per-story worktrees live here (mirrors build._WORKTREE_SUBDIR / the orphan
# sweeper) and are named ``agent-<short_run>-<story_id>``.
_WORKTREE_SUBDIR = (".claude", "worktrees")
_BRANCH_PREFIX = "feature/"
# Ledger run statuses that mean "a build is still alive here", so this run's
# transcripts/branches are kept even when the registry lacks a live pid (e.g. a
# markdown build that never registered). A genuinely crashed run is caught by
# the registry+pid path, not by this conservative status set.
_LEDGER_LIVE_STATUSES = {"IN_PROGRESS", "RATE_LIMITED"}


@dataclass
class CleanItem:
    """One thing ``clean`` considered: a worktree, a branch, or a log dir.

    ``kind`` is ``worktree`` | ``branch`` | ``logs``; ``name`` is the human label
    (branch name, worktree directory name, or run id); ``path`` is the filesystem
    target where one applies; ``reason`` explains why it is a candidate or why it
    was protected; ``removed`` flips True only after an actual ``--force`` removal.
    """

    kind: str
    name: str
    reason: str
    path: str | None = None
    removed: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "reason": self.reason,
            "path": self.path,
            "removed": self.removed,
        }


@dataclass
class CleanPlan:
    """The full plan: what would (or did) get removed, and what was protected."""

    root: str
    forced: bool = False
    candidates: list[CleanItem] = field(default_factory=list)
    protected: list[CleanItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[CleanItem]:
        return [c for c in self.candidates if c.kind == kind]

    @property
    def worktrees(self) -> list[CleanItem]:
        return self.by_kind("worktree")

    @property
    def branches(self) -> list[CleanItem]:
        return self.by_kind("branch")

    @property
    def logs(self) -> list[CleanItem]:
        return self.by_kind("logs")

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def removed_count(self) -> int:
        return sum(1 for c in self.candidates if c.removed)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "forced": self.forced,
            "candidates": [c.to_dict() for c in self.candidates],
            "protected": [c.to_dict() for c in self.protected],
            "errors": list(self.errors),
            "total": self.total,
            "removed": self.removed_count,
        }


# --- live-run discovery (registry + pid) ------------------------------------


def _live_runs(registry: Registry, root: Path) -> tuple[set[str], set[str]]:
    """Run ids + short prefixes of runs *alive* in this repo (registry + pid).

    A run is live iff its registry record is for this repo, has no
    ``finished_at``, and its pid answers a liveness probe. This is the
    differentiator over the blunt orphan sweeper: a crashed run (ledger still
    ``IN_PROGRESS`` but dead pid) is *not* live, so its leftovers are reclaimable,
    while a genuinely running build — here or in another session/clone sharing
    this path — is always spared. Best-effort: a missing/corrupt registry yields
    empty sets (nothing protected on that axis), never an exception.
    """
    try:
        root_key = str(root.resolve())
    except OSError:
        root_key = str(root)
    full: set[str] = set()
    prefixes: set[str] = set()
    for rec in registry.records():
        try:
            same_repo = str(Path(rec.repo).resolve()) == root_key
        except OSError:
            same_repo = rec.repo == root_key
        if not same_repo:
            continue
        if rec.finished_at:
            continue
        if not pid_alive(rec.pid):
            continue
        full.add(rec.run_id)
        prefixes.add(rec.run_id.split("-")[0])
    return full, prefixes


def _ledger_story_index(ledger: Ledger) -> tuple[set[str], set[str]]:
    """``(done_sids, live_run_ids)`` from the ledger across all known runs.

    ``done_sids`` is every story id any run marked ``DONE`` — the ledger half of
    the merge signal (squash-merge-correct, unlike ``git branch --merged``).
    ``live_run_ids`` are runs whose ledger status is still ``IN_PROGRESS`` /
    ``RATE_LIMITED`` — a conservative keep-set for transcripts/branches when the
    registry has no entry. Empty when no ledger exists yet.
    """
    done: set[str] = set()
    live: set[str] = set()
    try:
        runs = ledger.list_runs(limit=10_000)
    except Exception:  # pragma: no cover - a damaged ledger must not crash clean
        return done, live
    for run in runs:
        if run.get("status") in _LEDGER_LIVE_STATUSES:
            live.add(run["id"])
        try:
            rows = ledger.story_rows(run["id"])
        except Exception:  # pragma: no cover
            rows = []
        for row in rows:
            if row.get("status") == "DONE":
                done.add(row["story_id"])
    return done, live


def _live_story_ids(ledger: Ledger, live_run_ids: set[str]) -> set[str]:
    """Story ids a live run currently holds ``IN_PROGRESS`` (branch protection)."""
    sids: set[str] = set()
    for run_id in live_run_ids:
        try:
            rows = ledger.story_rows(run_id)
        except Exception:  # pragma: no cover
            rows = []
        for row in rows:
            if row.get("status") == "IN_PROGRESS":
                sids.add(row["story_id"])
    return sids


# --- worktrees --------------------------------------------------------------


def _registered_worktrees(root: Path) -> list[tuple[Path, bool]]:
    """``(path, locked)`` for every worktree git tracks (the first is the main one)."""
    try:
        res = _git(root, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out: list[tuple[Path, bool]] = []
    cur: Path | None = None
    locked = False
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            if cur is not None:
                out.append((cur, locked))
            cur = Path(line.removeprefix("worktree "))
            locked = False
        elif line.startswith("locked"):
            locked = True
    if cur is not None:
        out.append((cur, locked))
    return out


def _is_dirty(path: Path) -> bool:
    """Whether ``path`` has uncommitted changes.

    Returns True (treat as dirty → protect) when the check cannot run, so an
    un-inspectable checkout is never removed.
    """
    try:
        res = _git(path, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return True
    if res.returncode != 0:
        return True
    return bool(res.stdout.strip())


def _agent_prefix(name: str) -> str | None:
    """The owning run's short prefix from an ``agent-<prefix>-<story>`` dir name."""
    parts = name.split("-")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[1]
    return None


def _collect_worktrees(
    root: Path, live_prefixes: set[str], plan: CleanPlan
) -> None:
    """Add agent-* worktree candidates/protected entries to ``plan``.

    A worktree is reclaimable only when it is **not dirty** and its owning run is
    terminal or its pid is dead (AC4). The main worktree and the cwd are always
    spared; a checkout owned by a live run (or locked by one) is left alone.
    """
    worktrees_dir = root.joinpath(*_WORKTREE_SUBDIR)
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = root.resolve()

    registered = _registered_worktrees(root)
    main_path = registered[0][0].resolve() if registered else root.resolve()
    seen: set[Path] = set()

    for raw_path, locked in registered:
        path = raw_path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        seen.add(resolved)
        name = path.name
        if _agent_prefix(name) is None:
            continue  # not a controller worktree — never our business
        if resolved == main_path or resolved == cwd:
            plan.protected.append(
                CleanItem("worktree", name, "active worktree (main/cwd)", str(path))
            )
            continue
        prefix = _agent_prefix(name)
        if prefix in live_prefixes:
            why = "locked by live run" if locked else "owned by live run"
            plan.protected.append(CleanItem("worktree", name, why, str(path)))
            continue
        if not path.exists():
            plan.candidates.append(
                CleanItem("worktree", name, "registered worktree missing on disk", str(path))
            )
            continue
        if _is_dirty(path):
            plan.protected.append(
                CleanItem("worktree", name, "uncommitted changes (dirty)", str(path))
            )
            continue
        plan.candidates.append(
            CleanItem(
                "worktree", name, "orphaned worktree (owning run terminal/dead)", str(path)
            )
        )

    # Untracked debris: an `agent-*` directory git no longer registers (a crash
    # git could not deregister). The orphan sweeper rm -rf's these; we do too,
    # but never one a live run owns.
    if worktrees_dir.is_dir():
        try:
            entries = sorted(worktrees_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                resolved = entry
            if resolved in seen:
                continue
            if _agent_prefix(entry.name) is None:
                continue
            prefix = _agent_prefix(entry.name)
            if prefix in live_prefixes:
                plan.protected.append(
                    CleanItem("worktree", entry.name, "owned by live run", str(entry))
                )
                continue
            plan.candidates.append(
                CleanItem(
                    "worktree", entry.name, "stale worktree directory (untracked)", str(entry)
                )
            )


# --- branches ---------------------------------------------------------------


def _gh_branch_merged(branch: str, root: Path) -> bool:
    """Whether ``branch`` has a MERGED PR per ``gh`` (read-only, never mutates).

    Best-effort: returns False — never raises — when ``gh`` is absent,
    unauthenticated, offline, or no merged PR matches the head ref. This is the
    PR-merge-state half of the merge signal the story mandates (squash-merge
    correct, unlike ``git branch --merged``).
    """
    try:
        out = subprocess.run(
            [
                "gh", "pr", "list", "--head", branch, "--state", "merged",
                "--json", "number", "-q", ".[0].number", "--limit", "1",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def _local_feature_branches(root: Path) -> list[str]:
    try:
        res = _git(root, "branch", "--format=%(refname:short)")
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    return [
        ln.strip()
        for ln in res.stdout.splitlines()
        if ln.strip().startswith(_BRANCH_PREFIX)
    ]


def _current_branch(root: Path) -> str | None:
    try:
        res = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _collect_branches(
    root: Path,
    done_sids: set[str],
    live_sids: set[str],
    gh_merged_fn,
    plan: CleanPlan,
) -> None:
    """Add merged ``feature/<id>`` branch candidates / protected entries.

    "Merged" is the ledger (``status=DONE``) OR the PR's ``MERGED`` state — never
    ``git branch --merged``, which misreports squash-merged branches as unmerged
    (the 0-of-18 observation in the story). A branch whose story a live run still
    holds ``IN_PROGRESS``, and the currently checked-out branch, are never
    candidates.
    """
    current = _current_branch(root)
    for branch in _local_feature_branches(root):
        sid = branch[len(_BRANCH_PREFIX):]
        if branch == current:
            plan.protected.append(
                CleanItem("branch", branch, "current branch", None)
            )
            continue
        if sid in live_sids:
            plan.protected.append(
                CleanItem("branch", branch, "live run owns this story", None)
            )
            continue
        ledger_done = sid in done_sids
        gh_merged = False
        if not ledger_done:
            # Only spend a gh round-trip when the ledger has not already settled it.
            try:
                gh_merged = bool(gh_merged_fn(branch, root))
            except Exception:  # pragma: no cover - gh helper is best-effort
                gh_merged = False
        if ledger_done or gh_merged:
            signal = "ledger DONE" if ledger_done else "PR MERGED"
            plan.candidates.append(
                CleanItem("branch", branch, f"merged ({signal})", None)
            )
        else:
            plan.protected.append(
                CleanItem("branch", branch, "not merged (no DONE ledger / MERGED PR)", None)
            )


# --- transcript logs --------------------------------------------------------


def _collect_logs(
    db_path: Path, live_run_ids: set[str], plan: CleanPlan
) -> None:
    """Add stale ``<db>.logs/<run_id>`` transcript dirs; keep live runs' logs."""
    logs_root = Path(f"{db_path}.logs")
    if not logs_root.is_dir():
        return
    try:
        entries = sorted(logs_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        run_id = entry.name
        if run_id in live_run_ids:
            plan.protected.append(
                CleanItem("logs", run_id, "transcripts of a live run", str(entry))
            )
            continue
        plan.candidates.append(
            CleanItem("logs", run_id, "stale transcripts (run terminal)", str(entry))
        )


# --- public API -------------------------------------------------------------


def plan_clean(
    *,
    root: Path | None = None,
    db_path: Path | None = None,
    registry: Registry | None = None,
    gh_merged_fn=None,
) -> CleanPlan:
    """Compute (but never apply) the clean plan for ``root``.

    Gathers reclaimable orphan ``agent-*`` worktrees, squash-merged
    ``feature/<id>`` branches, and stale transcript log dirs — each cross-checked
    against the run registry + live-pid so a build active here or in another
    session is never disturbed. Pure: it reads git, the ledger, the registry and
    (optionally) ``gh``; it removes nothing and never touches the remote.
    """
    root = (root or Path.cwd())
    db_path = db_path or default_db_path(root)
    registry = registry or Registry()
    gh_merged_fn = gh_merged_fn or _gh_branch_merged

    ledger = Ledger(db_path)
    try:
        ledger.ensure_migrated()
    except Exception:  # pragma: no cover - a read-only plan must not fail on schema
        pass

    _full_live, live_prefixes = _live_runs(registry, root)
    done_sids, ledger_live_runs = _ledger_story_index(ledger)
    # Logs/branches are kept for any run still live by registry pid OR by an
    # un-registered ledger status — the conservative union.
    live_run_ids = _full_live | ledger_live_runs
    live_sids = _live_story_ids(ledger, live_run_ids)

    plan = CleanPlan(root=str(root))
    _collect_worktrees(root, live_prefixes, plan)
    _collect_branches(root, done_sids, live_sids, gh_merged_fn, plan)
    _collect_logs(db_path, live_run_ids, plan)
    return plan


def _delete_branch(root: Path, branch: str) -> bool:
    """Delete a local branch, leaving its tip reachable via reflog (recoverable).

    ``git branch -D`` removes only the ref; the commit object survives in the
    reflog/object store until gc, so the deletion is recoverable (AC5). Never
    touches the remote.
    """
    try:
        res = _git(root, "branch", "-D", branch)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def run_clean(
    *,
    root: Path | None = None,
    db_path: Path | None = None,
    registry: Registry | None = None,
    gh_merged_fn=None,
    force: bool = False,
) -> CleanPlan:
    """Plan a clean and, when ``force`` is set, apply it.

    Dry-run by default (``force=False``): returns the plan untouched. With
    ``force=True`` each candidate is removed — worktrees via the shared
    :func:`remove_story_worktree` (preserves the branch), branches via a
    reflog-recoverable ``git branch -D``, log dirs via ``rmtree`` — its
    ``removed`` flag is set, and any failure is appended to ``plan.errors``
    without aborting the rest. No step ever pushes to or fetches from the remote.
    """
    root = (root or Path.cwd())
    plan = plan_clean(
        root=root, db_path=db_path, registry=registry, gh_merged_fn=gh_merged_fn
    )
    if not force:
        return plan

    plan.forced = True
    for item in plan.candidates:
        try:
            if item.kind == "worktree":
                ok = remove_story_worktree(root, Path(item.path)) if item.path else False
            elif item.kind == "branch":
                ok = _delete_branch(root, item.name)
            elif item.kind == "logs":
                if item.path:
                    shutil.rmtree(item.path, ignore_errors=True)
                    ok = not Path(item.path).exists()
                else:
                    ok = False
            else:  # pragma: no cover - kinds are closed
                ok = False
        except (OSError, subprocess.SubprocessError) as exc:
            ok = False
            plan.errors.append(f"{item.kind} {item.name}: {exc}")
        item.removed = ok
        if not ok and not any(item.name in e for e in plan.errors):
            plan.errors.append(f"{item.kind} {item.name}: removal failed")
    return plan
