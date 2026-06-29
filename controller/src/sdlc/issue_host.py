# ABOUTME: Code-host adapter — one stable interface, a GitHub (`gh`) and a GitLab (`glab`) backend.
# ABOUTME: Story 22.2-001 — the same `sdlc issues …` ops route to either host; differences hidden.

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "GITHUB",
    "GITLAB",
    "SUPPORTED_HOSTS",
    "IssueHostError",
    "RunResult",
    "Issue",
    "IssueHostAdapter",
    "GitHubAdapter",
    "GitLabAdapter",
    "get_adapter",
    "host_from_remote",
    "detect_host",
    "resolve_host",
]

# The two code hosts this epic targets. GitHub is FX's personal forge (where the
# framework was built); GitLab is the company corporate standard. Values are the
# inventory's `host` column literals (Story 22.1-001).
GITHUB = "github"
GITLAB = "gitlab"
SUPPORTED_HOSTS = (GITHUB, GITLAB)

# A network call to either CLI can hang; cap each so a wedged request can never
# stall a batched mirror/sync pass.
_CLI_TIMEOUT = 30.0

# Parse the issue *ref* out of a created-issue URL — GitHub `.../issues/123`,
# GitLab `.../-/issues/5`. The ref is the GitHub issue *number* or the GitLab
# per-project *iid*, normalised to a string behind `issue_ref` (Story 22.1-001).
_ISSUE_REF_RE = re.compile(r"/issues/(\d+)")


class IssueHostError(Exception):
    """An unsupported/unauthenticated host, or a failed host CLI call."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of one host-CLI invocation."""

    returncode: int
    stdout: str
    stderr: str


# A runner runs one CLI argv and returns its :class:`RunResult`. Injected into
# adapters so tests can stub `gh`/`glab` without a live CLI, mirroring the
# dispatch-seam philosophy of Epic-20 (swap the CLI behind a stable interface).
Runner = Callable[[Sequence[str]], RunResult]


@dataclass(frozen=True)
class Issue:
    """A host issue, normalised across GitHub and GitLab.

    ``ref`` is the GitHub issue *number* or the GitLab *iid* as a string — the
    value the inventory stores in `issue_ref` alongside `host`. `state` is
    normalised to ``open``/``closed`` (GitHub `OPEN`/`CLOSED`, GitLab
    `opened`/`closed`); `assignees` to a tuple of login/username strings.
    ``body`` and ``labels`` are populated by :meth:`IssueHostAdapter.issue_view`
    (the reconcile path reads them to push the managed block without clobbering
    human content and to pull human labels); other verbs leave them empty.
    """

    host: str
    ref: str
    url: str | None = None
    title: str | None = None
    state: str | None = None
    assignees: tuple[str, ...] = ()
    body: str | None = None
    labels: tuple[str, ...] = ()


def _default_runner(argv: Sequence[str], timeout: float = _CLI_TIMEOUT) -> RunResult:
    """Run ``argv`` via subprocess; raise :class:`IssueHostError` if the CLI is absent."""
    try:
        out = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise IssueHostError(f"{argv[0]} not found on PATH — install the host CLI") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise IssueHostError(f"{argv[0]} invocation failed: {exc}") from exc
    return RunResult(returncode=out.returncode, stdout=out.stdout, stderr=out.stderr)


# --- host auto-detection -----------------------------------------------------


def _remote_url(root: str | Path) -> str | None:
    """``git remote get-url origin`` for ``root``, or None when unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


# scp-like remote: git@host:owner/sub/repo.git
_SCP_REMOTE = re.compile(r"^[\w.-]+@([\w.-]+):")
# url remote: ssh|https|http://[user[:pw]@]host[:port]/owner/…
_URL_REMOTE = re.compile(r"^(?:ssh|https?)://(?:[^@/]+@)?([\w.-]+)")


def host_from_remote(remote: str | None) -> str | None:
    """Map a git remote URL to ``github``/``gitlab`` by hostname, or None.

    Matches the hostname substring so self-hosted GitLab (e.g.
    ``gitlab.corp.internal``) still resolves; anything else returns None and the
    caller must pass an explicit override.
    """
    if not remote:
        return None
    remote = remote.strip()
    host = None
    for pattern in (_SCP_REMOTE, _URL_REMOTE):
        m = pattern.match(remote)
        if m:
            host = m.group(1).lower()
            break
    if host is None:
        return None
    if "github" in host:
        return GITHUB
    if "gitlab" in host:
        return GITLAB
    return None


def detect_host(root: str | Path) -> str | None:
    """Auto-detect the code host from ``root``'s ``origin`` remote, or None."""
    return host_from_remote(_remote_url(root))


def resolve_host(root: str | Path, override: str | None = None) -> str:
    """Pick the host: explicit ``override`` wins, else auto-detect from the remote.

    Fails fast with a clear message when the host cannot be determined or is
    unsupported, so a command never silently targets the wrong forge.
    """
    host = (override or detect_host(root) or "").lower()
    if not host:
        raise IssueHostError(
            "could not determine code host from git remote; "
            "pass an explicit host (github|gitlab)"
        )
    if host not in SUPPORTED_HOSTS:
        raise IssueHostError(
            f"unsupported host {host!r}; supported hosts: {', '.join(SUPPORTED_HOSTS)}"
        )
    return host


# --- the adapter interface ---------------------------------------------------


def _ref_of(ref: "str | Issue") -> str:
    return ref.ref if isinstance(ref, Issue) else str(ref)


class IssueHostAdapter(ABC):
    """One stable issue interface; a GitHub and a GitLab implementation behind it.

    Each verb takes/returns host-neutral values (`Issue`, the `issue_ref`
    string) so the same `sdlc issues …` command works on either host. The CLI
    `runner` is injected so the host call is the single seam to stub in tests.
    """

    host: str
    cli: str

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner or _default_runner

    # -- shared close-keyword (host-correct form) --
    def close_keyword(self, ref: "str | Issue") -> str:
        """The PR/MR close-link for an issue ref.

        Both GitHub PRs and GitLab MRs accept ``Closes #N`` to auto-close an
        issue in the same project on merge, so the form is shared.
        """
        return f"Closes #{_ref_of(ref)}"

    # -- the abstract verbs --
    @abstractmethod
    def whoami(self) -> str:
        """Resolve the authenticated user's login/username."""

    @abstractmethod
    def ensure_ready(self) -> str:
        """Verify the CLI is installed and authenticated; return the login or raise."""

    @abstractmethod
    def ensure_labels(self, labels: Sequence[str]) -> None:
        """Create each taxonomy label on the host if absent (idempotent).

        `sdlc issues init` calls this to provision the board's taxonomy (`story`,
        ``epic:NN``, ``feature:NN.F``, ``points:N``, ``risk:*``) *before* the
        backfill, so ``issue_create --label`` never fails against a fresh repo
        whose labels do not yet exist (Story 22.3-001 AC: init provisions the
        board + taxonomy). Re-running, or a label a human already created, is a
        harmless no-op.
        """

    @abstractmethod
    def issue_create(
        self,
        title: str,
        body: str,
        labels: Sequence[str] | None = None,
        assignee: str | None = None,
    ) -> Issue:
        """Create an issue; return it with its `issue_ref` populated."""

    @abstractmethod
    def issue_update(
        self,
        ref: "str | Issue",
        title: str | None = None,
        body: str | None = None,
        labels: Sequence[str] | None = None,
        remove_labels: Sequence[str] | None = None,
    ) -> Issue:
        """Update an existing issue's title/body, adding ``labels`` and removing ``remove_labels``.

        ``remove_labels`` (Story 22.4-002) lets a caller *swap* a label set — e.g.
        move the live ``status:<slug>`` label forward as the build advances —
        rather than only accreting labels.
        """

    @abstractmethod
    def issue_comment(self, ref: "str | Issue", body: str) -> None:
        """Post a comment on an issue, attributed to the running CLI's identity.

        Story 22.4-002: the build loop posts a short status comment per stage
        transition; the host's own auth (the developer's ``gh``/``glab`` login)
        is the attribution, so no shared bot token is involved.
        """

    @abstractmethod
    def issue_assign(self, ref: "str | Issue", assignee: str) -> Issue:
        """Assign an issue to a single user (Free-tier: one assignee)."""

    @abstractmethod
    def user_exists(self, user: str) -> bool:
        """Report whether ``user`` is a real account on the host.

        The assign command validates the target user once, up front, so an unknown
        user fails fast rather than partially assigning a cascade (Story 22.5-002).
        """

    @abstractmethod
    def issue_close(self, ref: "str | Issue") -> Issue:
        """Close an issue."""

    @abstractmethod
    def issue_find(self, marker: str) -> Issue | None:
        """Find a managed issue by its hidden ``<!-- sdlc-story: <id> -->`` marker."""

    @abstractmethod
    def issue_view(self, ref: "str | Issue") -> Issue:
        """Fetch one issue *with* its `body`, `labels`, and `assignees` populated.

        The reconcile path (Story 22.4-001) reads the live body to regenerate only
        the managed block (preserving human content outside it) and the live
        labels/assignees to pull human signals. Raises :class:`IssueHostError`
        when the ref no longer resolves (deleted on the host)."""

    # -- shared call plumbing --
    def _invoke(self, *args: str) -> RunResult:
        """Run ``<cli> ARGS`` through the runner; a missing/broken CLI raises IssueHostError."""
        try:
            return self._runner([self.cli, *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise IssueHostError(f"{self.cli} invocation failed: {exc}") from exc

    def _run(self, *args: str) -> RunResult:
        """Run ``<cli> ARGS``, raising :class:`IssueHostError` on a non-zero exit."""
        result = self._invoke(*args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise IssueHostError(f"{self.cli} {args[0]} failed: {detail}")
        return result

    def _ensure_ready(self, login_hint: str) -> str:
        """Shared `ensure_ready`: check `<cli> auth status`, then `whoami`."""
        status = self._invoke("auth", "status")
        if status.returncode != 0:
            raise IssueHostError(
                f"not authenticated to {self.host}; run `{login_hint}`"
            )
        return self.whoami()


def _ref_from_url(url: str) -> str | None:
    m = _ISSUE_REF_RE.search(url)
    return m.group(1) if m else None


# A 6-hex colour (no leading `#`) per taxonomy category, so the provisioned board
# reads at a glance: structural labels share a hue, risk is graded green→red. An
# unrecognised name falls back to a neutral grey (matches GitHub's default).
def _label_color(name: str) -> str:
    if name == "story":
        return "5319e7"  # purple — the framework-managed marker
    if name.startswith("epic:"):
        return "0e8a16"  # green
    if name.startswith("feature:"):
        return "1d76db"  # blue
    if name.startswith("points:"):
        return "fbca04"  # amber
    if name.startswith("risk:"):
        return {"risk:low": "c2e0c6", "risk:medium": "fbca04", "risk:high": "d93f0b"}.get(
            name.lower(), "ededed"
        )
    return "ededed"  # neutral grey


# --- GitHub (gh) -------------------------------------------------------------


class GitHubAdapter(IssueHostAdapter):
    """GitHub backend — routes every verb through the `gh` CLI."""

    host = GITHUB
    cli = "gh"

    def whoami(self) -> str:
        return self._run("api", "user", "--jq", ".login").stdout.strip()

    def ensure_ready(self) -> str:
        return self._ensure_ready("gh auth login")

    def ensure_labels(self, labels: Sequence[str]) -> None:
        # `gh label create --force` is create-or-update, so it is idempotent: a
        # missing label is created, an existing one (a prior init, or a label a
        # human already made) is harmlessly refreshed rather than erroring.
        for name in dict.fromkeys(labels):  # de-dupe, preserve order
            self._run("label", "create", name, "--color", _label_color(name), "--force")

    def issue_create(
        self,
        title: str,
        body: str,
        labels: Sequence[str] | None = None,
        assignee: str | None = None,
    ) -> Issue:
        args = ["issue", "create", "--title", title, "--body", body]
        for label in labels or []:
            args += ["--label", label]
        if assignee:
            args += ["--assignee", assignee]
        url = self._run(*args).stdout.strip()
        ref = _ref_from_url(url)
        if ref is None:
            raise IssueHostError(f"gh issue create returned no issue URL: {url!r}")
        return Issue(host=self.host, ref=ref, url=url, title=title, state="open",
                     assignees=(assignee,) if assignee else ())

    def issue_update(
        self,
        ref: "str | Issue",
        title: str | None = None,
        body: str | None = None,
        labels: Sequence[str] | None = None,
        remove_labels: Sequence[str] | None = None,
    ) -> Issue:
        ref = _ref_of(ref)
        args = ["issue", "edit", ref]
        if title is not None:
            args += ["--title", title]
        if body is not None:
            args += ["--body", body]
        for label in labels or []:
            args += ["--add-label", label]
        for label in remove_labels or []:
            args += ["--remove-label", label]
        url = self._run(*args).stdout.strip() or None
        return Issue(host=self.host, ref=ref, url=url, title=title)

    def issue_comment(self, ref: "str | Issue", body: str) -> None:
        self._run("issue", "comment", _ref_of(ref), "--body", body)

    def issue_assign(self, ref: "str | Issue", assignee: str) -> Issue:
        ref = _ref_of(ref)
        self._run("issue", "edit", ref, "--add-assignee", assignee)
        return Issue(host=self.host, ref=ref, assignees=(assignee,))

    def user_exists(self, user: str) -> bool:
        # `gh api users/<login>` is 200 for a real account, 404 (non-zero) for an
        # unknown one — so a clean exit means the user exists. Use `_invoke` (not
        # `_run`) so a 404 reads as "no such user" rather than raising.
        return self._invoke("api", f"users/{user}").returncode == 0

    def issue_close(self, ref: "str | Issue") -> Issue:
        ref = _ref_of(ref)
        self._run("issue", "close", ref)
        return Issue(host=self.host, ref=ref, state="closed")

    def issue_find(self, marker: str) -> Issue | None:
        out = self._run(
            "issue", "list", "--state", "all", "--search", marker,
            "--json", "number,url,title,state,body,assignees", "--limit", "50",
        ).stdout
        for row in _parse_json_array(out):
            if marker in (row.get("body") or ""):
                return Issue(
                    host=self.host,
                    ref=str(row.get("number")),
                    url=row.get("url"),
                    title=row.get("title"),
                    state=_norm_state(row.get("state")),
                    assignees=tuple(a.get("login") for a in row.get("assignees") or []),
                )
        return None

    def issue_view(self, ref: "str | Issue") -> Issue:
        ref = _ref_of(ref)
        out = self._run(
            "issue", "view", ref,
            "--json", "number,url,title,state,body,labels,assignees",
        ).stdout
        row = _parse_json_object(out)
        if not row:
            raise IssueHostError(f"gh issue view {ref} returned no issue")
        return Issue(
            host=self.host,
            ref=str(row.get("number") or ref),
            url=row.get("url"),
            title=row.get("title"),
            state=_norm_state(row.get("state")),
            body=row.get("body"),
            labels=_label_names(row.get("labels")),
            assignees=tuple(a.get("login") for a in row.get("assignees") or []),
        )


# --- GitLab (glab) -----------------------------------------------------------


class GitLabAdapter(IssueHostAdapter):
    """GitLab backend — routes every verb through the `glab` CLI.

    Targets GitLab Free/Core: a single assignee, labels for taxonomy (no native
    epics/weight). `glab` is GitLab's official CLI (issues, MRs, auth).
    """

    host = GITLAB
    cli = "glab"

    def whoami(self) -> str:
        return self._run("api", "user", "--jq", ".username").stdout.strip()

    def ensure_ready(self) -> str:
        return self._ensure_ready("glab auth login")

    def ensure_labels(self, labels: Sequence[str]) -> None:
        # `glab label create` has no create-or-update flag and errors when the
        # label already exists, so tolerate that one case to stay idempotent; any
        # other failure (auth, network) still raises. Colour takes a leading `#`.
        for name in dict.fromkeys(labels):  # de-dupe, preserve order
            result = self._invoke(
                "label", "create", "--name", name, "--color", f"#{_label_color(name)}"
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").lower()
                if "already" in detail or "taken" in detail:
                    continue  # label exists → idempotent no-op
                raise IssueHostError(f"glab label create failed: {detail.strip()}")

    def issue_create(
        self,
        title: str,
        body: str,
        labels: Sequence[str] | None = None,
        assignee: str | None = None,
    ) -> Issue:
        args = ["issue", "create", "--title", title, "--description", body, "--yes"]
        for label in labels or []:
            args += ["--label", label]
        if assignee:
            args += ["--assignee", assignee]
        url = self._run(*args).stdout.strip()
        ref = _ref_from_url(url)
        if ref is None:
            raise IssueHostError(f"glab issue create returned no issue URL: {url!r}")
        return Issue(host=self.host, ref=ref, url=url, title=title, state="open",
                     assignees=(assignee,) if assignee else ())

    def issue_update(
        self,
        ref: "str | Issue",
        title: str | None = None,
        body: str | None = None,
        labels: Sequence[str] | None = None,
        remove_labels: Sequence[str] | None = None,
    ) -> Issue:
        ref = _ref_of(ref)
        args = ["issue", "update", ref]
        if title is not None:
            args += ["--title", title]
        if body is not None:
            args += ["--description", body]
        for label in labels or []:
            args += ["--label", label]
        for label in remove_labels or []:
            args += ["--unlabel", label]
        self._run(*args)
        return Issue(host=self.host, ref=ref, title=title)

    def issue_comment(self, ref: "str | Issue", body: str) -> None:
        self._run("issue", "note", _ref_of(ref), "--message", body)

    def issue_assign(self, ref: "str | Issue", assignee: str) -> Issue:
        ref = _ref_of(ref)
        self._run("issue", "update", ref, "--assignee", assignee)
        return Issue(host=self.host, ref=ref, assignees=(assignee,))

    def user_exists(self, user: str) -> bool:
        # GitLab has no per-login endpoint; `glab api users?username=<u>` returns a
        # (possibly empty) array — a non-empty result means the username resolves.
        result = self._invoke("api", f"users?username={user}")
        if result.returncode != 0:
            return False
        return bool(_parse_json_array(result.stdout))

    def issue_close(self, ref: "str | Issue") -> Issue:
        ref = _ref_of(ref)
        self._run("issue", "close", ref)
        return Issue(host=self.host, ref=ref, state="closed")

    def issue_find(self, marker: str) -> Issue | None:
        out = self._run(
            "issue", "list", "--search", marker, "--all", "--output", "json",
        ).stdout
        for row in _parse_json_array(out):
            if marker in (row.get("description") or ""):
                return Issue(
                    host=self.host,
                    ref=str(row.get("iid")),
                    url=row.get("web_url"),
                    title=row.get("title"),
                    state=_norm_state(row.get("state")),
                    assignees=tuple(a.get("username") for a in row.get("assignees") or []),
                )
        return None

    def issue_view(self, ref: "str | Issue") -> Issue:
        ref = _ref_of(ref)
        out = self._run("issue", "view", ref, "--output", "json").stdout
        row = _parse_json_object(out)
        if not row:
            raise IssueHostError(f"glab issue view {ref} returned no issue")
        return Issue(
            host=self.host,
            ref=str(row.get("iid") or ref),
            url=row.get("web_url"),
            title=row.get("title"),
            state=_norm_state(row.get("state")),
            body=row.get("description"),
            labels=_label_names(row.get("labels")),
            assignees=tuple(a.get("username") for a in row.get("assignees") or []),
        )


# --- shared parsing helpers --------------------------------------------------


def _parse_json_array(stdout: str | None) -> list[dict]:
    """Parse a CLI's `--json`/`--output json` array; empty list on bad/empty output."""
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _parse_json_object(stdout: str | None) -> dict:
    """Parse a CLI's single-issue `--json`/`--output json` object; empty dict on bad/empty output."""
    if not stdout:
        return {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _label_names(labels: object) -> tuple[str, ...]:
    """Normalise a host's label list to a tuple of names.

    GitHub `--json labels` yields objects (`{"name": …}`); GitLab `--output json`
    yields plain strings. Accept either (and a string-keyed object) so the same
    reconcile reads labels off both hosts.
    """
    if not isinstance(labels, list):
        return ()
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


def _norm_state(state: str | None) -> str | None:
    """Normalise GitHub `OPEN`/`CLOSED` and GitLab `opened`/`closed` to `open`/`closed`."""
    if not state:
        return None
    s = state.lower()
    if s in ("open", "opened"):
        return "open"
    if s in ("close", "closed"):
        return "closed"
    return s


def get_adapter(host: str, runner: Runner | None = None) -> IssueHostAdapter:
    """Build the adapter for ``host`` (``github``/``gitlab``); raise if unsupported."""
    host = (host or "").lower()
    if host == GITHUB:
        return GitHubAdapter(runner=runner)
    if host == GITLAB:
        return GitLabAdapter(runner=runner)
    raise IssueHostError(
        f"unsupported host {host!r}; supported hosts: {', '.join(SUPPORTED_HOSTS)}"
    )
