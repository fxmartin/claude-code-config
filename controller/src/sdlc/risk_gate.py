# ABOUTME: High-risk file pattern detection for the human-approval merge gate.
# ABOUTME: Story 8.2-001 — loads glob patterns, matches changed files, supports per-repo overrides.

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# The default config ships inside the `sdlc` package (`sdlc/config/`) and is
# bundled into the wheel the same way the JSON schemas are, so it resolves in
# BOTH layouts: the controller checkout (editable install — CI workflow + merge
# agent) AND a `uv tool install`ed wheel where the source tree is gone.
# `importlib.resources` returns a Traversable; `uv tool install` unzips the wheel
# onto disk, so converting it to a Path via `str()` yields a real filesystem
# path. The config file remains the single source of truth shared with the
# GitHub workflow.
DEFAULT_CONFIG_PATH: Path = Path(
    str(resources.files(__package__) / "config" / "high-risk-patterns.yaml")
)

# The additive per-repo override file a consumer repo may ship at its root.
OVERRIDE_FILENAME = ".sdlc-risk-config.yaml"

# The single top-level key both the default config and overrides use.
PATTERNS_KEY = "high_risk_patterns"

# The label applied to any change request with a high-risk match.
RISK_LABEL = "risk:high"

# The maintainer approval signal for the high-risk gate (Story 8.2-001 GitHub
# path; Story 23.5-001 GitLab path). A maintainer (write+ access) applying this
# label clears the gate. On GitLab Free/Core — which has no `risk-approver`
# team-review path — it is the *only* approval signal, so it must be a
# first-class board label there. Mirrors `RISK_APPROVED_LABEL` in
# `.github/workflows/risk-gate.yml`.
RISK_APPROVED_LABEL = "risk-approved"

# The gate's operational labels, provisioned on the board so both the flag
# (`risk:high`) and the approval signal (`risk-approved`) always exist for a
# maintainer to apply — independent of whether a seeded story is high-risk.
GATE_LABELS = (RISK_LABEL, RISK_APPROVED_LABEL)


class RiskGateError(Exception):
    """A risk-gate config could not be read or had the wrong shape."""


def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-style glob into an anchored regex.

    Semantics deliberately match the GitHub workflow and the config comments:
    ``**`` crosses path separators (and an optional leading ``**/`` also matches
    at the repo root), while ``*`` and ``?`` stay within a single segment.
    """
    # Normalise a leading `**/` so it also matches zero leading directories
    # (e.g. `**/migrations/**` matches `migrations/x` at the root).
    if pattern.startswith("**/"):
        prefix = "(?:.*/)?"
        rest = pattern[3:]
    else:
        prefix = ""
        rest = pattern

    out: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if ch == "*":
            if i + 1 < n and rest[i + 1] == "*":
                # `**` — match across separators. Consume an optional trailing
                # slash so `**/foo` and `**foo` both behave.
                out.append(".*")
                i += 2
                if i < n and rest[i] == "/":
                    i += 1
            else:
                # `*` — match within a single path segment (no separator).
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return f"^{prefix}{''.join(out)}$"


@lru_cache(maxsize=None)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(_glob_to_regex(pattern))


def matches_pattern(path: str, pattern: str) -> bool:
    """Return True if ``path`` matches a single gitignore-style ``pattern``."""
    return _compiled(pattern).match(path) is not None


def _read_patterns_file(path: Path) -> list[str]:
    """Read and validate a `high_risk_patterns` list from a YAML file."""
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise RiskGateError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or PATTERNS_KEY not in data:
        raise RiskGateError(f"{path} must define a top-level {PATTERNS_KEY!r} key.")
    patterns = data[PATTERNS_KEY]
    if not isinstance(patterns, list) or not all(
        isinstance(p, str) for p in patterns
    ):
        raise RiskGateError(f"{path}: {PATTERNS_KEY!r} must be a list of strings.")
    return patterns


def load_patterns(
    *,
    config_path: Path | None = None,
    override_path: Path | None = None,
) -> list[str]:
    """Load the high-risk patterns, applying an additive per-repo override.

    The override is *additive*: any patterns it lists are appended to the
    defaults (de-duplicated, order preserved), never replacing them. A missing
    override file is silently ignored.
    """
    base = _read_patterns_file(config_path or DEFAULT_CONFIG_PATH)
    patterns = list(base)
    if override_path is not None and override_path.is_file():
        for extra in _read_patterns_file(override_path):
            if extra not in patterns:
                patterns.append(extra)
    return patterns


def match_high_risk(
    changed_files: list[str],
    *,
    config_path: Path | None = None,
    override_path: Path | None = None,
) -> dict[str, str]:
    """Return a mapping of each high-risk changed file to the pattern it hit.

    A file is reported once, keyed by the first pattern that matched. Files
    matching no pattern are omitted, so an empty result means the change set is
    clean.
    """
    patterns = load_patterns(config_path=config_path, override_path=override_path)
    matched: dict[str, str] = {}
    for path in changed_files:
        for pattern in patterns:
            if matches_pattern(path, pattern):
                matched[path] = pattern
                break
    return matched
