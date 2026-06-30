# ABOUTME: GitLab/GitHub auth & CI tokens — local CLI identity for runs, CI/CD env tokens for jobs.
# ABOUTME: Story 23.6-001 — never a committed secret; tokens redacted in logs the same on both hosts.

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from sdlc.issue_host import GITHUB, GITLAB, SUPPORTED_HOSTS, IssueHostError

__all__ = [
    "GITHUB",
    "GITLAB",
    "REDACTED",
    "TOKEN_ENV_VARS",
    "TOKEN_SCOPES",
    "HostAuthError",
    "CiToken",
    "resolve_local_login",
    "resolve_ci_token",
    "token_scopes",
    "redact",
    "find_committed_tokens",
]


class HostAuthError(Exception):
    """Auth could not be established, or a host/token request was malformed."""


# CI/CD environment variables that may carry a token, in resolution *priority*
# order per host. Local runs do NOT consult these — they use the developer's
# `gh`/`glab` CLI login (see :func:`resolve_local_login`); these are only the
# CI-side fallback (Story 23.6-001 AC2). `GITLAB_TOKEN`/`GH_TOKEN` are the CLIs'
# own conventional names (a project access token or CI/CD variable); GitLab's
# `CI_JOB_TOKEN` is the runner-injected, job-scoped token of last resort. None of
# these is ever read from a committed file — only the process environment.
TOKEN_ENV_VARS: dict[str, tuple[str, ...]] = {
    GITHUB: ("GH_TOKEN", "GITHUB_TOKEN"),
    GITLAB: ("GITLAB_TOKEN", "GL_TOKEN", "CI_JOB_TOKEN"),
}

# The minimal token scopes the CI-side actions (release tag + pipeline status)
# need — documented so an adopter provisions a least-privilege project access
# token, not an owner PAT. GitLab `api` covers releases/MR status; `write_repository`
# covers the tag push. GitHub's classic `repo` scope is the equivalent grant.
TOKEN_SCOPES: dict[str, tuple[str, ...]] = {
    GITHUB: ("repo",),
    GITLAB: ("api", "write_repository"),
}

# Placeholder substituted for any masked token. A single stable marker so log
# consumers can tell a credential was elided rather than the field being empty.
REDACTED = "[redacted]"

# The credential *shapes* both hosts emit, recognised so a token that leaks into
# a log line (or, via :func:`find_committed_tokens`, into tracked source) is
# caught regardless of which host produced it — the "same protections" guarantee
# of AC3. GitHub classic PAT (`ghp_` + 36), GitHub fine-grained (`github_pat_…`),
# GitLab PAT (`glpat-` + 20). Bounded so an unrelated identifier is not masked.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20}"),
)


class _ReadyAdapter(Protocol):
    """The slice of :class:`IssueHostAdapter` this module needs: `ensure_ready`."""

    def ensure_ready(self) -> str: ...


@dataclass(frozen=True)
class CiToken:
    """A token resolved from the CI/CD environment for a host-side action.

    ``value`` is excluded from the dataclass ``repr`` (and from ``str``) so the
    secret never lands in a traceback, log line, or ledger record by accident —
    only ``host`` and the ``source`` env-var name are shown (Story 23.6-001 AC3).
    Read ``.value`` explicitly when the token must be handed to a CLI call.
    """

    host: str
    source: str
    value: str = field(repr=False)

    def masked(self) -> str:
        """The display form of the token — always :data:`REDACTED`."""
        return REDACTED

    def __str__(self) -> str:
        return f"CiToken(host={self.host!r}, source={self.source!r}, value={REDACTED})"


def resolve_local_login(adapter: _ReadyAdapter) -> str:
    """Resolve the developer's login from their local CLI auth (Story 23.6-001 AC1).

    A *local* run acts as the developer: it verifies the `gh`/`glab` CLI is
    installed and authenticated and returns the login (used for attribution, per
    Epic-22 identity). There is no shared service token — each developer is
    themselves. Raises :class:`HostAuthError` (not the lower-level
    :class:`IssueHostError`) so callers get one auth-failure type, with the host
    CLI's own "run `glab auth login`" hint preserved.
    """
    try:
        login = adapter.ensure_ready()
    except IssueHostError as exc:
        raise HostAuthError(str(exc)) from exc
    login = (login or "").strip()
    if not login:
        raise HostAuthError("host auth returned a blank login; re-authenticate the CLI")
    return login


def _require_host(host: str) -> str:
    host = (host or "").lower()
    if host not in SUPPORTED_HOSTS:
        raise HostAuthError(
            f"unsupported host {host!r}; supported hosts: {', '.join(SUPPORTED_HOSTS)}"
        )
    return host


def resolve_ci_token(host: str, env: Mapping[str, str] | None = None) -> CiToken | None:
    """Resolve a CI-side token from the process environment (Story 23.6-001 AC2).

    Reads only from ``env`` (defaulting to :data:`os.environ`) — never a committed
    file — trying each :data:`TOKEN_ENV_VARS` name for the host in priority order
    and skipping any that are unset or blank. Returns the first match as a
    :class:`CiToken`, or ``None`` when no token is configured (the caller decides
    whether that is fatal). A blank-but-defined CI/CD variable is treated as
    absent, so an empty variable never shadows a real one further down the list.
    """
    host = _require_host(host)
    environ = os.environ if env is None else env
    for name in TOKEN_ENV_VARS[host]:
        value = (environ.get(name) or "").strip()
        if value:
            return CiToken(host=host, source=name, value=value)
    return None


def token_scopes(host: str) -> tuple[str, ...]:
    """The minimal token scopes for a host's CI-side actions (Story 23.6-001)."""
    return TOKEN_SCOPES[_require_host(host)]


def redact(text: str, *secrets: str | None) -> str:
    """Mask any token in ``text`` before it is logged (Story 23.6-001 AC3).

    Masks both the known credential *shapes* (so a leaked PAT from either host is
    caught) and any explicit ``secrets`` literals the caller passes — used for a
    shapeless value such as a ``CI_JOB_TOKEN``, where the exact string is the only
    thing to redact. Longest secrets first so a value that contains another is
    masked whole. Empty/``None`` secrets are ignored.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def find_committed_tokens(text: str) -> list[str]:
    """Return token-shaped literals in ``text`` — the no-secret-committed tripwire.

    A real credential pasted into tracked source matches a :data:`_TOKEN_PATTERNS`
    shape; an env-var *reference* (``$CI_JOB_TOKEN``, ``${GITLAB_TOKEN}``) does
    not, so the legitimate "read the token from a CI/CD variable" pattern passes
    clean while a hardcoded secret is flagged (Story 23.6-001 AC2/AC3).
    """
    found: list[str] = []
    for pattern in _TOKEN_PATTERNS:
        found.extend(pattern.findall(text))
    return found
