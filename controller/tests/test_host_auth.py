# ABOUTME: Tests for host_auth — local gh/glab auth identity + CI token resolution + token redaction.
# ABOUTME: Story 23.6-001 — local-auth path, CI-token path, no-secret-committed check; both hosts.

from __future__ import annotations

import pytest

from sdlc import host_auth as ha
from sdlc.host_auth import (
    GITHUB,
    GITLAB,
    CiToken,
    HostAuthError,
    find_committed_tokens,
    redact,
    resolve_ci_token,
    resolve_local_login,
    token_scopes,
)
from sdlc.issue_host import IssueHostError


# --- a minimal adapter stub --------------------------------------------------


class FakeAdapter:
    """Stand-in for an IssueHostAdapter: `ensure_ready` returns a login or raises."""

    def __init__(self, login: str = "octocat", error: Exception | None = None):
        self._login = login
        self._error = error
        self.ensure_ready_calls = 0

    def ensure_ready(self) -> str:
        self.ensure_ready_calls += 1
        if self._error is not None:
            raise self._error
        return self._login


# --- AC1: local run uses the developer's gh/glab auth identity ---------------


def test_resolve_local_login_returns_authenticated_login() -> None:
    adapter = FakeAdapter(login="fxmartin")
    assert resolve_local_login(adapter) == "fxmartin"
    assert adapter.ensure_ready_calls == 1


def test_resolve_local_login_unauthenticated_raises_host_auth_error() -> None:
    adapter = FakeAdapter(error=IssueHostError("not authenticated to gitlab; run `glab auth login`"))
    with pytest.raises(HostAuthError, match="glab auth login"):
        resolve_local_login(adapter)


def test_resolve_local_login_blank_login_raises() -> None:
    adapter = FakeAdapter(login="   ")
    with pytest.raises(HostAuthError):
        resolve_local_login(adapter)


# --- AC2: CI actions read a token from CI/CD env vars, never a committed file -


# Synthetic, obviously-placeholder token shapes (all-`x` bodies) — they match the
# detector's prefix+length rules without being live credentials, so the secret-scan
# gate's placeholder allowlist (`x{8,}`) treats them as the example tokens they are.
_FAKE_GLPAT = "glpat-xxxxxxxxxxxxxxxxxxxx"  # glpat- + 20 chars
_FAKE_GHP = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # ghp_ + 36 chars


def test_resolve_ci_token_gitlab_priority_order() -> None:
    # GITLAB_TOKEN (a project access token / CI/CD variable) wins over CI_JOB_TOKEN.
    env = {"CI_JOB_TOKEN": "job-tok", "GITLAB_TOKEN": _FAKE_GLPAT}
    token = resolve_ci_token(GITLAB, env=env)
    assert token is not None
    assert token.host == GITLAB
    assert token.source == "GITLAB_TOKEN"
    assert token.value == _FAKE_GLPAT


def test_resolve_ci_token_gitlab_falls_back_to_ci_job_token() -> None:
    token = resolve_ci_token(GITLAB, env={"CI_JOB_TOKEN": "ci-job-token-value"})
    assert token is not None
    assert token.source == "CI_JOB_TOKEN"
    assert token.value == "ci-job-token-value"


def test_resolve_ci_token_github_priority_order() -> None:
    env = {"GITHUB_TOKEN": "github-tok", "GH_TOKEN": "gh-tok"}
    token = resolve_ci_token(GITHUB, env=env)
    assert token is not None
    assert token.source == "GH_TOKEN"
    assert token.value == "gh-tok"


def test_resolve_ci_token_absent_returns_none() -> None:
    assert resolve_ci_token(GITLAB, env={}) is None
    assert resolve_ci_token(GITHUB, env={"UNRELATED": "x"}) is None


def test_resolve_ci_token_blank_value_is_ignored() -> None:
    # A defined-but-empty CI/CD variable must not be mistaken for a real token.
    token = resolve_ci_token(GITLAB, env={"GITLAB_TOKEN": "  ", "CI_JOB_TOKEN": "real"})
    assert token is not None
    assert token.source == "CI_JOB_TOKEN"


def test_resolve_ci_token_unsupported_host_raises() -> None:
    with pytest.raises(HostAuthError, match="unsupported host"):
        resolve_ci_token("bitbucket", env={"X": "y"})


# --- AC3: a present token is handled under the same protections both hosts ----


def test_ci_token_value_never_appears_in_repr_or_str() -> None:
    token = CiToken(host=GITLAB, source="GITLAB_TOKEN", value=_FAKE_GLPAT)
    assert _FAKE_GLPAT not in repr(token)
    assert _FAKE_GLPAT not in str(token)
    # The source env var is still visible for debugging; only the value is hidden.
    assert "GITLAB_TOKEN" in repr(token)
    assert token.masked() == ha.REDACTED


def test_redact_masks_gitlab_and_github_token_shapes_identically() -> None:
    text = f"github={_FAKE_GHP} gitlab={_FAKE_GLPAT}"
    out = redact(text)
    assert _FAKE_GHP not in out
    assert _FAKE_GLPAT not in out
    assert out.count(ha.REDACTED) == 2


def test_redact_masks_explicit_secret_literals() -> None:
    # A CI_JOB_TOKEN value has no fixed shape, so callers pass it explicitly.
    out = redact("authorization: Bearer ci-job-token-xyz", "ci-job-token-xyz")
    assert "ci-job-token-xyz" not in out
    assert ha.REDACTED in out


def test_redact_ignores_empty_secrets() -> None:
    assert redact("nothing secret here", "", None) == "nothing secret here"


# --- AC2/AC3: no-secret-committed check --------------------------------------


def test_find_committed_tokens_flags_token_shaped_literals() -> None:
    blob = f"TOKEN = '{_FAKE_GLPAT}'\nGH = {_FAKE_GHP}"
    found = find_committed_tokens(blob)
    assert _FAKE_GLPAT in found
    assert _FAKE_GHP in found


def test_find_committed_tokens_clean_content_is_empty() -> None:
    assert find_committed_tokens("export GITLAB_TOKEN=\"$CI_JOB_TOKEN\"  # from CI/CD vars") == []


# --- minimal token scopes per host -------------------------------------------


def test_token_scopes_gitlab_minimal() -> None:
    scopes = token_scopes(GITLAB)
    assert "api" in scopes
    assert "write_repository" in scopes


def test_token_scopes_github_minimal() -> None:
    assert "repo" in token_scopes(GITHUB)


def test_token_scopes_unsupported_host_raises() -> None:
    with pytest.raises(HostAuthError):
        token_scopes("bitbucket")
