# ABOUTME: Commit-message linter for agent-authored commits (Story 12.2-002).
# ABOUTME: Reads the repo's commitlint config and checks a faithful rule subset.

from __future__ import annotations

import json
import re
from pathlib import Path

# Conventional-commit header: ``type(scope)!: subject``. Scope and the breaking
# ``!`` are optional. A header that does not match leaves type/subject empty so
# the ``*-empty`` rules fire, mirroring how commitlint treats an unparseable
# header.
_HEADER_RE = re.compile(
    r"^(?P<type>[^(!:]*?)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<subject>.*)$"
)

# Config filenames commitlint itself searches, in the order we honour them. The
# ``package.json`` ``commitlint`` key is handled separately.
_CONFIG_FILENAMES = (
    ".commitlintrc.json",
    ".commitlintrc",
)


def load_commitlint_config(root: Path) -> dict | None:
    """Return the repo's commitlint config dict, or ``None`` when none exists.

    Searches ``root`` for a JSON ``.commitlintrc.json`` / ``.commitlintrc`` file,
    falling back to a ``commitlint`` key in ``package.json``. A missing config is
    a graceful no-op (the controller invents no rules); a malformed config is
    treated the same way rather than crashing a build. Only JSON forms are read —
    the controller never executes a ``commitlint.config.js`` for safety.
    """
    root = Path(root)
    for name in _CONFIG_FILENAMES:
        path = root / name
        if path.is_file():
            parsed = _read_json(path)
            if isinstance(parsed, dict):
                return parsed
    pkg = root / "package.json"
    if pkg.is_file():
        parsed = _read_json(pkg)
        if isinstance(parsed, dict) and isinstance(parsed.get("commitlint"), dict):
            return parsed["commitlint"]
    return None


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lint_commit_message(message: str, config: dict) -> list[str]:
    """Return a list of human-readable violations of ``config``'s commitlint rules.

    Implements the faithful subset of conventional rules the controller can apply
    deterministically without running Node: ``type-empty``, ``type-enum``,
    ``type-case``, ``scope-case``, ``subject-empty``, ``subject-case``,
    ``subject-full-stop``, ``header-max-length`` and ``body-leading-blank``. Only
    rules at error level (``2``) are enforced; disabled (``0``) and warn (``1``)
    levels, and any rule name not in the subset, are ignored — so an
    as-yet-unsupported rule never produces a spurious re-ask. An empty rule set
    yields no violations.
    """
    rules = (config or {}).get("rules") or {}
    lines = message.split("\n")
    header = lines[0] if lines else ""
    match = _HEADER_RE.match(header)
    ctype = (match.group("type") or "").strip() if match else ""
    scope = (match.group("scope") or "").strip() if match else None
    subject = (match.group("subject") or "").strip() if match else ""

    violations: list[str] = []

    def enforced(name: str) -> list | None:
        spec = rules.get(name)
        if isinstance(spec, list) and spec and spec[0] == 2:
            return spec
        return None

    if enforced("type-empty") and not ctype:
        violations.append("type-empty: a conventional type is required (type(scope): subject)")
    if (spec := enforced("type-enum")) and ctype:
        allowed = spec[2] if len(spec) > 2 and isinstance(spec[2], list) else []
        if allowed and ctype not in allowed:
            violations.append(f"type-enum: type '{ctype}' is not one of {allowed}")
    if enforced("type-case") and ctype and ctype != ctype.lower():
        violations.append(f"type-case: type '{ctype}' must be lower-case")
    if enforced("scope-case") and scope and scope != scope.lower():
        violations.append(f"scope-case: scope '{scope}' must be lower-case")
    if enforced("subject-empty") and not subject:
        violations.append("subject-empty: a subject is required")
    if enforced("subject-case") and subject and subject != subject.lower():
        violations.append(f"subject-case: subject '{subject}' must be lower-case")
    if enforced("subject-full-stop") and subject.endswith("."):
        violations.append("subject-full-stop: subject must not end with '.'")
    if (spec := enforced("header-max-length")) and len(spec) > 2:
        limit = spec[2]
        if isinstance(limit, int) and len(header) > limit:
            violations.append(
                f"header-max-length: header is {len(header)} chars (max {limit})"
            )
    if enforced("body-leading-blank") and len(lines) > 1 and lines[1].strip():
        violations.append("body-leading-blank: leave a blank line after the header")

    return violations
