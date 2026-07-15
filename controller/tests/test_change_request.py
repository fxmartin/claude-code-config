# ABOUTME: Tests for the change-request (PR/MR) adapter verbs — gh pr / glab mr.
# ABOUTME: Story 23.1-001; every gh/glab call is stubbed, so there is no live CLI dependency.

from __future__ import annotations

import json

import pytest

from sdlc import issue_host as ih


# --- a recording fake runner (mirrors test_issue_host.FakeRunner) ------------


class FakeRunner:
    """Record argv and return canned :class:`RunResult`s keyed by an argv needle."""

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


# --- ChangeRequest ref normalisation ----------------------------------------


def test_cr_ref_of_accepts_string() -> None:
    assert ih._cr_ref_of("42") == "42"


def test_cr_ref_of_accepts_change_request() -> None:
    cr = ih.ChangeRequest(host=ih.GITHUB, ref="7")
    assert ih._cr_ref_of(cr) == "7"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/fx/r/pull/123", "123"),
        ("https://gitlab.com/g/r/-/merge_requests/5", "5"),
        ("https://gitlab.corp.internal/team/r/-/merge_requests/88", "88"),
        ("https://github.com/fx/r/issues/9", None),  # an issue, not a CR
        ("no url at all", None),
    ],
)
def test_cr_ref_from_url(url, expected) -> None:
    assert ih._cr_ref_from_url(url) == expected


# --- GitHub adapter: every CR verb (gh pr) ----------------------------------


def test_github_cr_create() -> None:
    runner = FakeRunner({"pr create": (0, "https://github.com/fx/r/pull/123\n", "")})
    cr = ih.GitHubAdapter(runner=runner).cr_create(
        source_branch="feature/23.1-001",
        title="Story 23.1-001",
        body="Closes #42",
        target_branch="main",
    )
    assert cr.host == ih.GITHUB
    assert cr.ref == "123"
    assert cr.url == "https://github.com/fx/r/pull/123"
    assert cr.source_branch == "feature/23.1-001"
    assert cr.target_branch == "main"
    argv = runner.calls[-1]
    assert argv[:3] == ["gh", "pr", "create"]
    assert "--head" in argv and "feature/23.1-001" in argv
    assert "--base" in argv and "main" in argv
    assert "--title" in argv and "Story 23.1-001" in argv
    assert "--body" in argv and "Closes #42" in argv


def test_github_cr_create_without_target_omits_base() -> None:
    # No explicit target → let gh default to the repo's default branch.
    runner = FakeRunner({"pr create": (0, "https://github.com/fx/r/pull/9\n", "")})
    ih.GitHubAdapter(runner=runner).cr_create(
        source_branch="feature/x", title="t", body="b"
    )
    argv = runner.calls[-1]
    assert "--base" not in argv
    assert "--draft" not in argv


def test_github_cr_create_draft() -> None:
    runner = FakeRunner({"pr create": (0, "https://github.com/fx/r/pull/9\n", "")})
    ih.GitHubAdapter(runner=runner).cr_create(
        source_branch="feature/x", title="t", body="b", draft=True
    )
    assert "--draft" in runner.calls[-1]


def test_github_cr_create_no_url_raises() -> None:
    runner = FakeRunner({"pr create": (0, "no url here\n", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitHubAdapter(runner=runner).cr_create(
            source_branch="b", title="t", body="b"
        )
    assert "no change-request URL" in str(exc.value)


def test_github_cr_diff() -> None:
    diff = "diff --git a/x b/x\n+added\n"
    runner = FakeRunner({"pr diff": (0, diff, "")})
    assert ih.GitHubAdapter(runner=runner).cr_diff("123") == diff
    assert runner.calls[-1] == ["gh", "pr", "diff", "123"]


def test_github_cr_diff_accepts_change_request() -> None:
    runner = FakeRunner({"pr diff": (0, "d", "")})
    cr = ih.ChangeRequest(host=ih.GITHUB, ref="55")
    ih.GitHubAdapter(runner=runner).cr_diff(cr)
    assert runner.calls[-1] == ["gh", "pr", "diff", "55"]


def test_github_cr_status_success() -> None:
    rollup = json.dumps(
        {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]}
    )
    runner = FakeRunner({"pr view": (0, rollup, "")})
    assert ih.GitHubAdapter(runner=runner).cr_status("123") == ih.CR_SUCCESS
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "pr", "view", "123"]
    assert "--json" in argv and "statusCheckRollup" in argv


def test_github_cr_status_failed_when_any_check_fails() -> None:
    rollup = json.dumps(
        {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]}
    )
    runner = FakeRunner({"pr view": (0, rollup, "")})
    assert ih.GitHubAdapter(runner=runner).cr_status("123") == ih.CR_FAILED


def test_github_cr_status_pending_when_in_progress() -> None:
    rollup = json.dumps(
        {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]}
    )
    runner = FakeRunner({"pr view": (0, rollup, "")})
    assert ih.GitHubAdapter(runner=runner).cr_status("123") == ih.CR_PENDING


def test_github_cr_status_none_when_no_checks() -> None:
    runner = FakeRunner({"pr view": (0, json.dumps({"statusCheckRollup": []}), "")})
    assert ih.GitHubAdapter(runner=runner).cr_status("123") == ih.CR_NONE


def test_github_cr_merge() -> None:
    runner = FakeRunner({"pr merge": (0, "", "")})
    cr = ih.GitHubAdapter(runner=runner).cr_merge("123")
    assert cr.host == ih.GITHUB and cr.ref == "123" and cr.state == "merged"
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "pr", "merge", "123"]
    assert "--merge" in argv


def test_github_cr_url_from_change_request_is_offline() -> None:
    # A ChangeRequest already carrying its url needs no host call.
    runner = FakeRunner()
    cr = ih.ChangeRequest(host=ih.GITHUB, ref="9", url="https://github.com/fx/r/pull/9")
    assert ih.GitHubAdapter(runner=runner).cr_url(cr) == "https://github.com/fx/r/pull/9"
    assert runner.calls == []


def test_github_cr_url_queries_by_ref() -> None:
    runner = FakeRunner({"pr view": (0, "https://github.com/fx/r/pull/9\n", "")})
    assert ih.GitHubAdapter(runner=runner).cr_url("9") == "https://github.com/fx/r/pull/9"
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "pr", "view", "9"]
    assert "url" in argv


# --- GitLab adapter: every CR verb (glab mr) --------------------------------


def test_gitlab_cr_create() -> None:
    runner = FakeRunner(
        {"mr create": (0, "https://gitlab.com/g/r/-/merge_requests/5\n", "")}
    )
    cr = ih.GitLabAdapter(runner=runner).cr_create(
        source_branch="feature/23.1-001",
        title="Story 23.1-001",
        body="Closes #42",
        target_branch="main",
    )
    assert cr.host == ih.GITLAB
    assert cr.ref == "5"
    assert cr.url == "https://gitlab.com/g/r/-/merge_requests/5"
    assert cr.source_branch == "feature/23.1-001"
    assert cr.target_branch == "main"
    argv = runner.calls[-1]
    assert argv[:3] == ["glab", "mr", "create"]
    assert "--source-branch" in argv and "feature/23.1-001" in argv
    assert "--target-branch" in argv and "main" in argv
    assert "--title" in argv and "Story 23.1-001" in argv
    assert "--description" in argv and "Closes #42" in argv
    assert "--yes" in argv


def test_gitlab_cr_create_without_target_omits_target_branch() -> None:
    runner = FakeRunner({"mr create": (0, "https://gitlab.com/g/r/-/merge_requests/9\n", "")})
    ih.GitLabAdapter(runner=runner).cr_create(
        source_branch="feature/x", title="t", body="b"
    )
    argv = runner.calls[-1]
    assert "--target-branch" not in argv
    assert "--draft" not in argv


def test_gitlab_cr_create_draft() -> None:
    runner = FakeRunner({"mr create": (0, "https://gitlab.com/g/r/-/merge_requests/9\n", "")})
    ih.GitLabAdapter(runner=runner).cr_create(
        source_branch="feature/x", title="t", body="b", draft=True
    )
    assert "--draft" in runner.calls[-1]


def test_gitlab_cr_create_no_url_raises() -> None:
    runner = FakeRunner({"mr create": (0, "", "")})
    with pytest.raises(ih.IssueHostError) as exc:
        ih.GitLabAdapter(runner=runner).cr_create(
            source_branch="b", title="t", body="b"
        )
    assert "no change-request URL" in str(exc.value)


def test_gitlab_cr_diff() -> None:
    diff = "diff --git a/x b/x\n+added\n"
    runner = FakeRunner({"mr diff": (0, diff, "")})
    assert ih.GitLabAdapter(runner=runner).cr_diff("5") == diff
    assert runner.calls[-1] == ["glab", "mr", "diff", "5"]


def test_gitlab_cr_status_maps_pipeline_status() -> None:
    payload = json.dumps({"pipeline": {"status": "success"}})
    runner = FakeRunner({"mr view": (0, payload, "")})
    assert ih.GitLabAdapter(runner=runner).cr_status("5") == ih.CR_SUCCESS
    argv = runner.calls[-1]
    assert argv[:4] == ["glab", "mr", "view", "5"]
    assert "--output" in argv and "json" in argv


def test_gitlab_cr_status_failed() -> None:
    payload = json.dumps({"pipeline": {"status": "failed"}})
    runner = FakeRunner({"mr view": (0, payload, "")})
    assert ih.GitLabAdapter(runner=runner).cr_status("5") == ih.CR_FAILED


def test_gitlab_cr_status_running_is_pending() -> None:
    payload = json.dumps({"pipeline": {"status": "running"}})
    runner = FakeRunner({"mr view": (0, payload, "")})
    assert ih.GitLabAdapter(runner=runner).cr_status("5") == ih.CR_PENDING


def test_gitlab_cr_status_no_pipeline_is_none() -> None:
    # An MR on a project with no CI has a null pipeline → no gating signal.
    runner = FakeRunner({"mr view": (0, json.dumps({"pipeline": None}), "")})
    assert ih.GitLabAdapter(runner=runner).cr_status("5") == ih.CR_NONE


def test_gitlab_cr_merge() -> None:
    runner = FakeRunner({"mr merge": (0, "", "")})
    cr = ih.GitLabAdapter(runner=runner).cr_merge("5")
    assert cr.host == ih.GITLAB and cr.ref == "5" and cr.state == "merged"
    argv = runner.calls[-1]
    assert argv[:4] == ["glab", "mr", "merge", "5"]
    assert "--yes" in argv


def test_gitlab_cr_url_queries_by_ref() -> None:
    payload = json.dumps({"web_url": "https://gitlab.com/g/r/-/merge_requests/5"})
    runner = FakeRunner({"mr view": (0, payload, "")})
    assert (
        ih.GitLabAdapter(runner=runner).cr_url("5")
        == "https://gitlab.com/g/r/-/merge_requests/5"
    )


def test_gitlab_cr_url_from_change_request_is_offline() -> None:
    # A ChangeRequest already carrying its url needs no glab call (mirrors GitHub).
    runner = FakeRunner()
    cr = ih.ChangeRequest(
        host=ih.GITLAB, ref="5", url="https://gitlab.com/g/r/-/merge_requests/5"
    )
    assert (
        ih.GitLabAdapter(runner=runner).cr_url(cr)
        == "https://gitlab.com/g/r/-/merge_requests/5"
    )
    assert runner.calls == []


# --- cr_checks: labels + named per-check states (Story 25.1-001) -------------


def test_github_cr_checks_reads_labels_and_named_checks() -> None:
    payload = json.dumps({
        "labels": [{"name": "risk:high"}, {"name": "story"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "High-risk file approval gate",
             "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "tests",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ci/legacy",
             "state": "SUCCESS"},
        ],
    })
    runner = FakeRunner({"pr view": (0, payload, "")})
    view = ih.GitHubAdapter(runner=runner).cr_checks("123")
    assert view.labels == ("risk:high", "story")
    assert ("High-risk file approval gate", ih.CR_FAILED) in view.checks
    assert ("tests", ih.CR_SUCCESS) in view.checks
    # A StatusContext is named by its `context` field.
    assert ("ci/legacy", ih.CR_SUCCESS) in view.checks
    argv = runner.calls[-1]
    assert argv[:4] == ["gh", "pr", "view", "123"]
    assert "--json" in argv and "labels,statusCheckRollup" in argv


def test_github_cr_checks_empty_rollup() -> None:
    payload = json.dumps({"labels": [], "statusCheckRollup": []})
    runner = FakeRunner({"pr view": (0, payload, "")})
    view = ih.GitHubAdapter(runner=runner).cr_checks("123")
    assert view.labels == ()
    assert view.checks == ()


def test_gitlab_cr_checks_reads_labels_and_pipeline_jobs() -> None:
    mr = json.dumps({"labels": ["risk:high"], "pipeline": {"id": 77, "status": "failed"}})
    jobs = json.dumps([
        {"name": "risk-gate", "status": "failed"},
        {"name": "tests", "status": "success"},
    ])
    runner = FakeRunner({"mr view": (0, mr, ""), "api": (0, jobs, "")})
    view = ih.GitLabAdapter(runner=runner).cr_checks("5")
    assert view.labels == ("risk:high",)
    assert ("risk-gate", ih.CR_FAILED) in view.checks
    assert ("tests", ih.CR_SUCCESS) in view.checks
    # The jobs are read off the MR's head pipeline via the API.
    joined = " ".join(runner.calls[-1])
    assert "pipelines/77/jobs" in joined


def test_gitlab_cr_checks_no_pipeline_has_no_checks() -> None:
    runner = FakeRunner({"mr view": (0, json.dumps({"labels": ["a"], "pipeline": None}), "")})
    view = ih.GitLabAdapter(runner=runner).cr_checks("5")
    assert view.labels == ("a",)
    assert view.checks == ()


# --- normalisation helpers ---------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("success", ih.CR_SUCCESS),
        ("failed", ih.CR_FAILED),
        ("canceled", ih.CR_FAILED),
        ("running", ih.CR_PENDING),
        ("pending", ih.CR_PENDING),
        ("created", ih.CR_PENDING),
        ("preparing", ih.CR_PENDING),
        ("waiting_for_resource", ih.CR_PENDING),
        ("manual", ih.CR_PENDING),
        ("scheduled", ih.CR_PENDING),
        ("skipped", ih.CR_NONE),
        (None, ih.CR_NONE),
        ("", ih.CR_NONE),
        ("weird", ih.CR_UNKNOWN),
    ],
)
def test_gitlab_pipeline_status_mapping(raw, expected) -> None:
    assert ih._gitlab_pipeline_status(raw) == expected


@pytest.mark.parametrize(
    "rollup, expected",
    [
        ([], ih.CR_NONE),
        ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}], ih.CR_SUCCESS),
        ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}], ih.CR_FAILED),
        ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}], ih.CR_FAILED),
        ([{"__typename": "CheckRun", "status": "QUEUED", "conclusion": None}], ih.CR_PENDING),
        ([{"__typename": "StatusContext", "state": "SUCCESS"}], ih.CR_SUCCESS),
        ([{"__typename": "StatusContext", "state": "FAILURE"}], ih.CR_FAILED),
        ([{"__typename": "StatusContext", "state": "PENDING"}], ih.CR_PENDING),
        # failure dominates a mixed bag; pending dominates over success.
        (
            [
                {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
            ih.CR_FAILED,
        ),
        # a neutral/skipped-only rollup yields no gating signal.
        ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"}], ih.CR_NONE),
        # an unrecognised-only rollup is reported as unknown, not silently green.
        ([{"__typename": "StatusContext", "state": "MYSTERY"}], ih.CR_UNKNOWN),
    ],
)
def test_github_rollup_status(rollup, expected) -> None:
    assert ih._github_rollup_status(rollup) == expected


@pytest.mark.parametrize(
    "item, expected",
    [
        # A COMPLETED CheckRun with no conclusion carries no pass/fail signal.
        ({"status": "COMPLETED", "conclusion": None}, ih.CR_NONE),
        # A CheckRun still in flight (status not COMPLETED, none of the explicit
        # in-flight literals) defaults to pending rather than passing.
        ({"status": "REQUESTED_ACTION", "conclusion": None}, ih.CR_PENDING),
        # A StatusContext with an unrecognised state is unknown, not green.
        ({"status": None, "state": "MYSTERY"}, ih.CR_UNKNOWN),
    ],
)
def test_github_check_status(item, expected) -> None:
    assert ih._github_check_status(item) == expected


# --- get_adapter still routes both hosts (no regression) ---------------------


def test_cr_verbs_present_on_both_adapters() -> None:
    for host in (ih.GITHUB, ih.GITLAB):
        adapter = ih.get_adapter(host)
        for verb in ("cr_create", "cr_diff", "cr_status", "cr_merge", "cr_url"):
            assert callable(getattr(adapter, verb))


# --- change-request terms (Story 23.2-001) ----------------------------------


def test_github_cr_terms_phrase_a_pull_request() -> None:
    terms = ih.get_adapter(ih.GITHUB).cr_terms
    assert terms is ih.GITHUB_CR_TERMS
    assert terms.host == ih.GITHUB
    assert terms.abbr == "PR"
    assert terms.ref_noun == "PR number"
    # GitHub names no CLI in its prompt, so the hint is empty (byte-identical, AC2).
    assert terms.cli_hint == ""


def test_gitlab_cr_terms_phrase_a_merge_request() -> None:
    terms = ih.get_adapter(ih.GITLAB).cr_terms
    assert terms is ih.GITLAB_CR_TERMS
    assert terms.host == ih.GITLAB
    assert terms.abbr == "MR"
    assert terms.ref_noun == "MR iid"
    # The GitLab hint names the create CLI so the agent reaches for glab, not gh.
    assert "glab mr create" in terms.cli_hint
