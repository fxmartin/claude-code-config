# ABOUTME: Deterministic change-class detection — docs-only vs code (Story 27.2-001).
# ABOUTME: Classifies a story's built diff from git so docs-only changes can skip code-gate prices.

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from sdlc.risk_gate import matches_pattern

# The two change classes the controller recognises. ``docs-only`` skips the
# coverage dispatch and the adversarial slot (the non-adversarial review still
# runs); ``code`` — the conservative default — runs the full gate chain.
DOCS_ONLY = "docs-only"
CODE = "code"

# The built-in docs patterns (gitignore-style globs, matched with the same
# anchored semantics as risk_gate.py). ``**/*.md`` is the anchored spelling of
# "any markdown file at any depth" (including the repo root); ``docs/**``
# covers non-markdown assets under the docs tree. Story 27.2-003 references
# this same list for the fix-issue mirror — extend here, never fork it.
DEFAULT_DOCS_PATTERNS: tuple[str, ...] = ("**/*.md", "docs/**")

# The additive per-repo allowlist a consumer repo may ship at its root,
# mirroring the `.sdlc-*` override convention (`.sdlc-risk-config.yaml`,
# `.sdlc-model-routing.yaml`).
OVERRIDE_FILENAME = ".sdlc-change-class.yaml"

# The single top-level key the override file uses.
PATTERNS_KEY = "docs_patterns"

# Bounded so a wedged git call can never stall a build between stages.
_GIT_TIMEOUT_S = 30


class ChangeClassError(Exception):
    """A change-class allowlist override could not be read or had the wrong shape."""


def load_docs_patterns(*, root: Path | None = None) -> list[str]:
    """The docs patterns: built-in defaults plus the repo's additive allowlist.

    ``root`` is the working tree the override file is looked up in (the story
    worktree during a build); ``None`` skips the override lookup and returns the
    defaults. The override is *additive* — its patterns are appended to the
    defaults (de-duplicated, order preserved), never replacing them, so a repo
    can never accidentally widen ``docs-only`` by dropping a default. A missing
    file is silently ignored; a present-but-malformed file raises
    :class:`ChangeClassError` so a typo'd allowlist fails loudly rather than
    silently classifying everything as code.
    """
    patterns = list(DEFAULT_DOCS_PATTERNS)
    if root is None:
        return patterns
    override = Path(root) / OVERRIDE_FILENAME
    if not override.is_file():
        return patterns
    try:
        data = yaml.safe_load(override.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ChangeClassError(f"{override} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or PATTERNS_KEY not in data:
        raise ChangeClassError(
            f"{override} must define a top-level {PATTERNS_KEY!r} key."
        )
    extra = data[PATTERNS_KEY]
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        raise ChangeClassError(
            f"{override}: {PATTERNS_KEY!r} must be a list of strings."
        )
    for pattern in extra:
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def classify_files(
    files: list[str], *, patterns: list[str] | None = None
) -> str:
    """Classify a changed-file list as :data:`DOCS_ONLY` or :data:`CODE`.

    ``docs-only`` requires a non-empty list where *every* file matches a docs
    pattern; anything else — including an empty list, where there is no diff to
    verify — is ``code``, so the full gate chain is the failure mode, never the
    skip.
    """
    if not files:
        return CODE
    pats = list(patterns) if patterns is not None else load_docs_patterns()
    for path in files:
        if not any(matches_pattern(path, pattern) for pattern in pats):
            return CODE
    return DOCS_ONLY


def changed_files(root: Path, base_ref: str, branch: str) -> list[str]:
    """The files ``branch`` changed since it diverged from ``base_ref``.

    The deterministic classification feed (Story 27.2-001): read straight from
    ``git diff --name-only base...branch`` in the story worktree, never from the
    agent's self-reported result. Best-effort — any git failure (missing repo,
    unknown ref, timeout) returns an empty list, which classifies as ``code``
    so a broken lookup can only ever run *more* gates, not fewer.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...{branch}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
