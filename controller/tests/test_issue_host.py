# ABOUTME: Tests for issue_host — the code-host adapter (gh / glab) for the story mirror.
# ABOUTME: Story 22.2-001; every gh/glab call is stubbed, so there is no live CLI dependency.

from __future__ import annotations

import json

import pytest

from sdlc import issue_host as ih


# --- a recording fake runner -------------------------------------------------


class FakeRunner:
    """Record argv and return canned :class:`RunResult`s keyed by an argv needle.

    ``mapping`` keys are matched as a substring of the joined argv; the value is
    the ``RunResult`` (or a ``(returncode, stdout, stderr)`` tuple) to return. A
    bare default models the happy path; an unmatched call returns ``default``.
    """

    def __init__(self, mapping=None, default=(0, "", "")):
        self.mapping = mapping or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.mapping.items():
            if needle in joined:
                return _as_result(result)
        return _as_result(self.default)


def _as_result(value):
    if isinstance(value, ih.RunResult):
        return value
    rc, out, err = value
    return ih.RunResult(returncode=rc, stdout=out, stderr=err)


# --- host detection ----------------------------------------------------------


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("git@github.com:fxmartin/repo.git", ih.GITHUB),
        ("https://github.com/fxmartin/repo.git", ih.GITHUB),
        ("ssh://git@github.com/fxmartin/repo.git", ih.GITHUB),
        ("git@gitlab.com:group/sub/repo.git", ih.GITLAB),
        ("https://gitlab.example.com/group/repo.git", ih.GITLAB),
        ("https://gitlab.corp.internal/team/repo", ih.GITLAB),
        ("git@bitbucket.org:team/repo.git", None),
        ("", None),
        ("not-a-url", None),
    ],
)
def test_host_from_remote(remote, expected) -> None:
    assert ih.host_from_remote(remote) is expected


def test_detect_host_reads_origin(monkeypatch) -> None:
    monkeypatch.setattr(ih, "_remote_url", lambda root: "git@github.com:fx/r.git")
    assert ih.detect_host(".") == ih.GITHUB


def test_detect_host_no_remote(monkeypatch) -> None:
    monkeypatch.setattr(ih, "_remote_url", lambda root: None)
    assert ih.detect_host(".") is None


# --- host resolution + fail-fast --------------------------------------------


def test_resolve_host_override_wins(monkeypatch) -> None:
    monkeypatch.setattr(ih, "_remote_url", lambda root: "git@github.com:fx/r.git")
    # An explicit override beats the auto-detected GitHub remote.
    assert ih.resolve_host(".", override="gitlab") == ih.GITLAB


def test_resolve_host_auto_detect(monkeypatch) -> None:
    monkeypatch.setattr(ih, "_remote_url", lambda root: "git@gitlab.com:g/r.git")
    assert ih.resolve_host(".") == ih.GITLAB


def test_resolve_host_unknown_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(ih, "_remote_url", lambda root: "git@bitbucket.org:g/r.git")
    with pytest.raises(ih.IssueHostError) as exc:
        ih.resolve_host(".")
    assert "could not determine" in str(exc.value).lower()


def test_resolve_host_unsupported_override_fails_fast() -> None:
    with pytest.raises(ih.IssueHostError) as exc:
        ih.resolve_host(".", override="bitbucket")
    assert "unsupported" in str(exc.value).lower()


# --- adapter selection -------------------------------------------------------


def test_get_adapter_github() -> None:
    a = ih.get_adapter(ih.GITHUB)
    assert isinstance(a, ih.GitHubAdapter)
    assert a.host == ih.GITHUB and a.cli == "gh"


def test_get_adapter_gitlab() -> None:
    a = ih.get_adapter(ih.GITLAB)
    assert isinstance(a, ih.GitLabAdapter)
    assert a.host == ih.GITLAB and a.cli == "glab"


def test_get_adapter_unknown_raises() -> None:
    with pytest.raises(ih.IssueHostError):
        ih.get_adapter("bitbucket")


# --- close keyword (host-correct form) --------------------------------------


def test_close_keyword_github() -> None:
    assert ih.get_adapter(ih.GITHUB).close_keyword("42") == "Closes #42"


def test_close_keyword_gitlab() -> None:
    # GitLab MRs accept the same `Closes #N` form for issues in the same project.
    assert ih.get_adapter(ih.GITLAB).close_keyword("42") == "Closes #42"


def test_close_keyword_accepts_issue() -> None:
    issue = ih.Issue(host=ih.GITHUB, ref="7")
    assert ih.get_adapter(ih.GITHUB).close_keyword(issue) == "Closes #7"


# --- GitHub adapter: every verb ---------------------------------------------


def test_github_whoami() -> None:
    runner = FakeRunner({"api user": (0, "octocat\n", "")})
    assert ih.GitHubAdapter(runner=runner).whoami() == "octocat"
    assert runner.calls[-1] == ["gh", "api", "user", "--jq", ".login"]


def test_github_issue_create() -> None:
    runner = FakeRunner({"issue create": (0, "https://github.com/fx/r/issues/123\n", "")})
    issue = ih.GitHubAdapter(runner=runner).issue_create(
        title="Story 22.2-001", body="spec", labels=["story", "epic:22"], assignee="fx"
    )
    assert issue.ref == "123"
    assert issue.host == ih.GITHUB
    assert issue.url == "https://github.com/fx/r/issues/123"
    argv = runner.calls[-1]
    assert argv[:3] == ["gh", "issue", "create"]
    assert "--title" in argv and "Story 22.2-001" in argv
    assert "--body" in argv and "spec" in argv
    assert argv.count("--label") == 2
    assert "story" in argv and "epic:22" in argv
    assert "--assignee" in argv and "fx" in argv


def test_github_issue_update() -> None:
    runner = FakeRunner({"issue edit": (0, "https://github.com/fx/r/issues/9\n", "")})
    issue = ih.GitHubAdapter(runner=runner).issue_update(
        "9", title="new", body="newbody", labels=["points:5"]
    )
    assert issue.ref == "9"
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "issue", "edit", "9"]
    assert "--title" in argv and "--body" in argv
    assert "--add-label" in argv and "points:5" in argv


def test_github_issue_assign() -> None:
    runner = FakeRunner({"issue edit": (0, "", "")})
    ih.GitHubAdapter(runner=runner).issue_assign("9", "alice")
    assert runner.calls[-1] == ["gh", "issue", "edit", "9", "--add-assignee", "alice"]


def test_github_issue_close() -> None:
    runner = FakeRunner({"issue close": (0, "", "")})
    issue = ih.GitHubAdapter(runner=runner).issue_close("9")
    assert issue.ref == "9" and issue.state == "closed"
    assert runner.calls[-1] == ["gh", "issue", "close", "9"]


def test_github_issue_find_matches_marker() -> None:
    marker = "<!-- sdlc-story: 22.2-001 -->"
    payload = json.dumps(
        [
            {"number": 5, "url": "u5", "title": "other", "state": "OPEN",
             "body": "nothing here", "assignees": []},
            {"number": 7, "url": "u7", "title": "the one", "state": "OPEN",
             "body": f"head\n{marker}\ntail", "assignees": [{"login": "fx"}]},
        ]
    )
    runner = FakeRunner({"issue list": (0, payload, "")})
    issue = ih.GitHubAdapter(runner=runner).issue_find(marker)
    assert issue is not None
    assert issue.ref == "7"
    assert issue.state == "open"
    assert issue.assignees == ("fx",)


def test_github_issue_find_no_match_returns_none() -> None:
    runner = FakeRunner({"issue list": (0, "[]", "")})
    assert ih.GitHubAdapter(runner=runner).issue_find("<!-- sdlc-story: x -->") is None


# --- GitLab adapter: every verb ---------------------------------------------


def test_gitlab_whoami() -> None:
    runner = FakeRunner({"api user": (0, "rootuser\n", "")})
    assert ih.GitLabAdapter(runner=runner).whoami() == "rootuser"
    assert runner.calls[-1] == ["glab", "api", "user", "--jq", ".username"]


def test_gitlab_issue_create() -> None:
    runner = FakeRunner({"issue create": (0, "https://gitlab.com/g/r/-/issues/5\n", "")})
    issue = ih.GitLabAdapter(runner=runner).issue_create(
        title="t", body="d", labels=["story"], assignee="fx"
    )
    assert issue.ref == "5"
    assert issue.host == ih.GITLAB
    argv = runner.calls[-1]
    assert argv[:3] == ["glab", "issue", "create"]
    assert "--title" in argv and "--description" in argv
    assert "--yes" in argv
    assert "--label" in argv and "story" in argv
    assert "--assignee" in argv and "fx" in argv


def test_gitlab_issue_update() -> None:
    runner = FakeRunner({"issue update": (0, "", "")})
    ih.GitLabAdapter(runner=runner).issue_update("5", title="t", body="d", labels=["risk:high"])
    argv = runner.calls[-1]
    assert argv[:4] == ["glab", "issue", "update", "5"]
    assert "--title" in argv and "--description" in argv
    assert "--label" in argv and "risk:high" in argv


def test_gitlab_issue_assign() -> None:
    runner = FakeRunner({"issue update": (0, "", "")})
    ih.GitLabAdapter(runner=runner).issue_assign("5", "alice")
    assert runner.calls[-1] == ["glab", "issue", "update", "5", "--assignee", "alice"]


def test_gitlab_issue_close() -> None:
    runner = FakeRunner({"issue close": (0, "", "")})
    issue = ih.GitLabAdapter(runner=runner).issue_close("5")
    assert issue.ref == "5" and issue.state == "closed"
    assert runner.calls[-1] == ["glab", "issue", "close", "5"]


def test_gitlab_issue_find_matches_marker() -> None:
    marker = "<!-- sdlc-story: 22.2-001 -->"
    payload = json.dumps(
        [
            {"iid": 3, "web_url": "u3", "title": "no", "state": "opened",
             "description": "blah", "assignees": []},
            {"iid": 8, "web_url": "u8", "title": "yes", "state": "opened",
             "description": f"x {marker} y", "assignees": [{"username": "fx"}]},
        ]
    )
    runner = FakeRunner({"issue list": (0, payload, "")})
    issue = ih.GitLabAdapter(runner=runner).issue_find(marker)
    assert issue is not None
    assert issue.ref == "8"
    assert issue.state == "open"
    assert issue.assignees == ("fx",)


# --- issue_view: reconcile read path (body + labels + assignees) ------------
# Story 22.4-001: the reconcile reads the live issue to push only the managed
# block and to pull human labels/assignee. issue_view must populate body and
# labels (which the other verbs leave empty) off each host's JSON shape.


def test_github_issue_view_populates_body_and_labels() -> None:
    payload = json.dumps(
        {
            "number": 42,
            "url": "https://github.com/fx/r/issues/42",
            "title": "Story 22.4-001",
            "state": "OPEN",
            "body": "head\n<!-- sdlc-story: 22.4-001 -->\ntail",
            "labels": [{"name": "story"}, {"name": "wontfix"}],
            "assignees": [{"login": "fx"}],
        }
    )
    runner = FakeRunner({"issue view": (0, payload, "")})
    issue = ih.GitHubAdapter(runner=runner).issue_view("42")
    assert issue.host == ih.GITHUB
    assert issue.ref == "42"
    assert issue.url == "https://github.com/fx/r/issues/42"
    assert issue.state == "open"
    assert issue.body == "head\n<!-- sdlc-story: 22.4-001 -->\ntail"
    assert issue.labels == ("story", "wontfix")
    assert issue.assignees == ("fx",)
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "issue", "view", "42"]
    assert "--json" in argv
    assert "number,url,title,state,body,labels,assignees" in argv


def test_github_issue_view_accepts_issue_ref() -> None:
    payload = json.dumps({"number": 7, "url": "u7", "state": "OPEN",
                          "body": "b", "labels": [], "assignees": []})
    runner = FakeRunner({"issue view": (0, payload, "")})
    issue = ih.Issue(host=ih.GITHUB, ref="7")
    out = ih.GitHubAdapter(runner=runner).issue_view(issue)
    assert out.ref == "7"
    assert runner.calls[-1][:4] == ["gh", "issue", "view", "7"]


def test_github_issue_view_falls_back_to_ref_when_number_missing() -> None:
    # No usable number in the payload → ref falls back to the requested ref.
    payload = json.dumps({"url": "u", "state": "OPEN", "body": "b",
                          "labels": [], "assignees": []})
    runner = FakeRunner({"issue view": (0, payload, "")})
    assert ih.GitHubAdapter(runner=runner).issue_view("99").ref == "99"


def test_github_issue_view_empty_raises() -> None:
    runner = FakeRunner({"issue view": (0, "", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitHubAdapter(runner=runner).issue_view("13")
    assert "no issue" in str(exc.value).lower()


def test_gitlab_issue_view_populates_body_and_labels() -> None:
    payload = json.dumps(
        {
            "iid": 8,
            "web_url": "https://gitlab.com/g/r/-/issues/8",
            "title": "Story 22.4-001",
            "state": "opened",
            "description": "x\n<!-- sdlc-story: 22.4-001 -->\ny",
            "labels": ["story", "blocked"],
            "assignees": [{"username": "fx"}],
        }
    )
    runner = FakeRunner({"issue view": (0, payload, "")})
    issue = ih.GitLabAdapter(runner=runner).issue_view("8")
    assert issue.host == ih.GITLAB
    assert issue.ref == "8"
    assert issue.url == "https://gitlab.com/g/r/-/issues/8"
    assert issue.state == "open"
    # GitLab maps `description` → body and yields plain-string labels.
    assert issue.body == "x\n<!-- sdlc-story: 22.4-001 -->\ny"
    assert issue.labels == ("story", "blocked")
    assert issue.assignees == ("fx",)
    argv = runner.calls[-1]
    assert argv[:4] == ["glab", "issue", "view", "8"]
    assert "--output" in argv and "json" in argv


def test_gitlab_issue_view_falls_back_to_ref_when_iid_missing() -> None:
    payload = json.dumps({"web_url": "u", "state": "opened", "description": "b",
                          "labels": [], "assignees": []})
    runner = FakeRunner({"issue view": (0, payload, "")})
    assert ih.GitLabAdapter(runner=runner).issue_view("55").ref == "55"


def test_gitlab_issue_view_empty_raises() -> None:
    runner = FakeRunner({"issue view": (0, "", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitLabAdapter(runner=runner).issue_view("21")
    assert "no issue" in str(exc.value).lower()


# --- unauthenticated / failure fail-fast ------------------------------------


def test_ensure_ready_unauth_fails_fast_github() -> None:
    runner = FakeRunner({"auth status": (1, "", "not logged in")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitHubAdapter(runner=runner).ensure_ready()
    msg = str(exc.value).lower()
    assert "github" in msg and "auth" in msg


def test_ensure_ready_unauth_fails_fast_gitlab() -> None:
    runner = FakeRunner({"auth status": (1, "", "not logged in")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitLabAdapter(runner=runner).ensure_ready()
    msg = str(exc.value).lower()
    assert "gitlab" in msg and "auth" in msg


def test_ensure_ready_ok_returns_login() -> None:
    runner = FakeRunner({"auth status": (0, "", ""), "api user": (0, "fx\n", "")})
    assert ih.GitHubAdapter(runner=runner).ensure_ready() == "fx"


def test_nonzero_call_raises_with_stderr() -> None:
    runner = FakeRunner({"issue close": (1, "", "issue not found")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitHubAdapter(runner=runner).issue_close("999")
    assert "issue not found" in str(exc.value)


def test_missing_cli_raises(monkeypatch) -> None:
    # The default subprocess runner raises a clean IssueHostError if the CLI is absent.
    def boom(argv, timeout=None):
        raise FileNotFoundError(argv[0])

    a = ih.GitHubAdapter(runner=boom)
    with pytest.raises(ih.IssueHostError):
        a.whoami()


# --- issue_create with no parseable URL fails fast --------------------------


def test_github_issue_create_no_url_raises() -> None:
    runner = FakeRunner({"issue create": (0, "no url here\n", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitHubAdapter(runner=runner).issue_create(title="t", body="b")
    assert "no issue URL" in str(exc.value)


def test_gitlab_issue_create_no_url_raises() -> None:
    runner = FakeRunner({"issue create": (0, "", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitLabAdapter(runner=runner).issue_create(title="t", body="b")
    assert "no issue URL" in str(exc.value)


def test_gitlab_issue_find_no_match_returns_none() -> None:
    payload = json.dumps(
        [{"iid": 1, "web_url": "u", "title": "x", "state": "opened",
          "description": "unrelated", "assignees": []}]
    )
    runner = FakeRunner({"issue list": (0, payload, "")})
    assert ih.GitLabAdapter(runner=runner).issue_find("<!-- sdlc-story: z -->") is None


# --- shared parsing / state-normalisation helpers ----------------------------


@pytest.mark.parametrize("stdout", ["", None, "not json{", "{\"not\": \"a list\"}"])
def test_parse_json_array_bad_input_returns_empty(stdout) -> None:
    assert ih._parse_json_array(stdout) == []


def test_parse_json_array_valid_list() -> None:
    assert ih._parse_json_array('[{"a": 1}]') == [{"a": 1}]


@pytest.mark.parametrize("stdout", ["", None, "not json{", "[1, 2, 3]"])
def test_parse_json_object_bad_input_returns_empty(stdout) -> None:
    # Empty/garbage, and a JSON array (not an object) all collapse to {}.
    assert ih._parse_json_object(stdout) == {}


def test_parse_json_object_valid_object() -> None:
    assert ih._parse_json_object('{"number": 9, "state": "OPEN"}') == {
        "number": 9,
        "state": "OPEN",
    }


@pytest.mark.parametrize(
    "labels, expected",
    [
        # GitHub shape: list of objects keyed by name.
        ([{"name": "story"}, {"name": "wontfix"}], ("story", "wontfix")),
        # GitLab shape: list of plain strings.
        (["story", "blocked"], ("story", "blocked")),
        # Mixed + malformed entries are skipped, valid ones survive in order.
        (["a", {"name": "b"}, {"noname": "x"}, 7, {"name": 5}], ("a", "b")),
        ([], ()),
        # Non-list (None / object) → empty tuple.
        (None, ()),
        ({"name": "x"}, ()),
    ],
)
def test_label_names_normalises_both_host_shapes(labels, expected) -> None:
    assert ih._label_names(labels) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("OPEN", "open"),
        ("opened", "open"),
        ("CLOSED", "closed"),
        ("closed", "closed"),
        ("close", "closed"),
        ("weird", "weird"),
    ],
)
def test_norm_state(raw, expected) -> None:
    assert ih._norm_state(raw) == expected


# --- the default subprocess runner + remote reader (live, no host CLI) -------


def test_default_runner_runs_a_real_command() -> None:
    result = ih._default_runner(["printf", "hi"])
    assert result.returncode == 0
    assert result.stdout == "hi"


def test_default_runner_missing_binary_raises() -> None:
    with pytest.raises(ih.IssueHostError) as exc:
        ih._default_runner(["sdlc-no-such-binary-xyz"])
    assert "not found on PATH" in str(exc.value)


def test_default_runner_os_error_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(ih.subprocess, "run", boom)
    with pytest.raises(ih.IssueHostError) as exc:
        ih._default_runner(["whatever"])
    assert "invocation failed" in str(exc.value)


def test_remote_url_reads_origin(tmp_path) -> None:
    import subprocess as sp

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    sp.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
            "git@github.com:fx/r.git"], check=True)
    assert ih._remote_url(tmp_path) == "git@github.com:fx/r.git"


def test_remote_url_no_remote_returns_none(tmp_path) -> None:
    import subprocess as sp

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    # No origin configured → non-zero git exit → None.
    assert ih._remote_url(tmp_path) is None


def test_remote_url_git_failure_returns_none(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(ih.subprocess, "run", boom)
    assert ih._remote_url(".") is None


# --- issue_comment + status-label removal (Story 22.4-002) -------------------


def test_github_issue_comment() -> None:
    runner = FakeRunner({"issue comment": (0, "", "")})
    ih.GitHubAdapter(runner=runner).issue_comment("9", "status: building")
    assert runner.calls[-1] == ["gh", "issue", "comment", "9", "--body", "status: building"]


def test_gitlab_issue_comment() -> None:
    runner = FakeRunner({"issue note": (0, "", "")})
    ih.GitLabAdapter(runner=runner).issue_comment("5", "status: building")
    assert runner.calls[-1] == ["glab", "issue", "note", "5", "--message", "status: building"]


def test_github_issue_comment_accepts_issue_object() -> None:
    runner = FakeRunner({"issue comment": (0, "", "")})
    issue = ih.Issue(host=ih.GITHUB, ref="11")
    ih.GitHubAdapter(runner=runner).issue_comment(issue, "hi")
    assert runner.calls[-1][:4] == ["gh", "issue", "comment", "11"]


def test_github_issue_update_removes_labels() -> None:
    runner = FakeRunner({"issue edit": (0, "https://github.com/fx/r/issues/9\n", "")})
    ih.GitHubAdapter(runner=runner).issue_update(
        "9", labels=["status:building"], remove_labels=["status:in-review", "status:merging"],
    )
    argv = runner.calls[-1]
    assert "--add-label" in argv and "status:building" in argv
    assert argv.count("--remove-label") == 2
    assert "status:in-review" in argv and "status:merging" in argv


def test_gitlab_issue_update_unlabels() -> None:
    runner = FakeRunner({"issue update": (0, "", "")})
    ih.GitLabAdapter(runner=runner).issue_update(
        "5", labels=["status:building"], remove_labels=["status:in-review"],
    )
    argv = runner.calls[-1]
    assert "--label" in argv and "status:building" in argv
    assert "--unlabel" in argv and "status:in-review" in argv
