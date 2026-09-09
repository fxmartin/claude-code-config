# ABOUTME: Tests for build_issue — best-effort build-loop ↔ host-issue integration.
# ABOUTME: Story 22.4-002 — Closes #N close-link + live status comments/labels; never blocks a build.

from __future__ import annotations

import pytest

from sdlc import build_issue as bi
from sdlc import issue_host as ih
from sdlc.build import Ledger, BuildOptions, render_build_prompt, render_coverage_prompt
from sdlc.cohort import Story


# --- a recording fake runner (same shape as test_issue_host) -----------------


class FakeRunner:
    """Record argv and return canned RunResults keyed by an argv-substring needle."""

    def __init__(self, mapping=None, default=(0, "", "")):
        self.mapping = mapping or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.mapping.items():
            if needle in joined:
                rc, out, err = result
                return ih.RunResult(returncode=rc, stdout=out, stderr=err)
        rc, out, err = self.default
        return ih.RunResult(returncode=rc, stdout=out, stderr=err)


# --- fixtures ----------------------------------------------------------------


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _mapped(ledger: Ledger, story_id="22.4-002", host=ih.GITHUB, ref="42") -> None:
    """Project a spec row, then record its host issue mapping."""
    ledger.inventory_upsert_specs([(story_id, "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping(story_id, host, ref)


HOSTS = [ih.GITHUB, ih.GITLAB]


# --- close_link (AC1) --------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_close_link_for_mapped_story(tmp_path, host):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=host, ref="7")
    runner = FakeRunner()
    assert bi.close_link(ledger, "22.4-002", runner=runner) == "Closes #7"


def test_change_request_terms_match_mapped_host(tmp_path):
    """A story mapped to a host yields that host's CR terms (Story 23.2-001 AC1/AC2)."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="23.2-001", host=ih.GITLAB, ref="7")
    _mapped(ledger, story_id="22.4-002", host=ih.GITHUB, ref="9")
    assert bi.change_request_terms(ledger, "23.2-001") is ih.GITLAB_CR_TERMS
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_unmapped_defaults_to_github(tmp_path):
    """An unmapped story falls back to GitHub terms so its prompt is unchanged (AC2)."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_unsupported_host_defaults_to_github(tmp_path):
    """An unsupported recorded host degrades to GitHub terms, never raises."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping("22.4-002", "bitbucket", "9")
    assert bi.change_request_terms(ledger, "22.4-002") is ih.GITHUB_CR_TERMS


def test_change_request_terms_tolerates_broken_ledger():
    """A ledger stub lacking inventory_get_mapping must not crash the build."""
    class _NoInventory:
        pass

    assert bi.change_request_terms(_NoInventory(), "22.4-002") is ih.GITHUB_CR_TERMS  # type: ignore[arg-type]


def test_close_link_unmapped_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    # spec row exists but no host/issue_ref → unmapped.
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_no_row_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_unsupported_host_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    ledger.inventory_set_mapping("22.4-002", "bitbucket", "9")
    # An unsupported host must never raise — best-effort yields None.
    assert bi.close_link(ledger, "22.4-002") is None


def test_close_link_tolerates_broken_ledger():
    class _NoInventory:
        pass

    # A ledger stub lacking inventory_get_mapping must not crash the build.
    assert bi.close_link(_NoInventory(), "22.4-002") is None  # type: ignore[arg-type]


# --- announce_status (AC2) ---------------------------------------------------


def test_announce_status_posts_comment_and_label_github(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()

    applied = bi.announce_status(ledger, "22.4-002", "building", runner=runner)

    assert applied == "building"
    joined = [" ".join(c) for c in runner.calls]
    # A short comment on the issue, via the developer's own gh identity.
    assert any("gh issue comment 42" in c and "building" in c for c in joined)
    # A status:<slug> label stamped, prior status labels removed.
    edit = next(c for c in runner.calls if "edit" in c)
    assert "--add-label" in edit and "status:building" in edit
    assert "--remove-label" in edit and "status:in-review" in edit


@pytest.mark.parametrize("host,cli,comment_verb", [
    (ih.GITHUB, "gh", "comment"),
    (ih.GITLAB, "glab", "note"),
])
def test_announce_status_uses_host_comment_verb(tmp_path, host, cli, comment_verb):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=host, ref="5")
    runner = FakeRunner()
    bi.announce_status(ledger, "22.4-002", "in-review", runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any(f"{cli} issue {comment_verb} 5" in c for c in joined)


def test_announce_status_unmapped_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("22.4-002", "22", "22.4", "t", 5, "High")])
    runner = FakeRunner()
    assert bi.announce_status(ledger, "22.4-002", "building", runner=runner) is None
    assert runner.calls == []  # never touched the host


def test_announce_status_none_status_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger)
    runner = FakeRunner()
    assert bi.announce_status(ledger, "22.4-002", None, runner=runner) is None
    assert runner.calls == []


def test_announce_status_tolerates_host_failure(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    # Every host call fails — the build must continue regardless.
    runner = FakeRunner(default=(1, "", "boom"))
    # Does not raise; returns None on a fully-failed comment+label attempt.
    assert bi.announce_status(ledger, "22.4-002", "building", runner=runner) is None


def test_announce_status_comment_failure_does_not_block_label(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    # Comment fails but the label edit succeeds — independent best-effort lanes.
    runner = FakeRunner(mapping={"issue comment": (1, "", "no perms")})
    applied = bi.announce_status(ledger, "22.4-002", "building", runner=runner)
    assert applied == "building"
    assert any("edit" in c for c in runner.calls)


def test_announce_status_tolerates_broken_ledger():
    class _NoInventory:
        pass

    # A ledger stub lacking inventory_get_mapping must not crash the build — the
    # lookup itself raising is swallowed to a logged no-op (mirrors close_link).
    assert bi.announce_status(_NoInventory(), "22.4-002", "building") is None  # type: ignore[arg-type]


# --- stage / terminal status mapping ----------------------------------------


@pytest.mark.parametrize("stage,slug", [
    ("build", "building"),
    ("coverage", "building"),
    ("review", "in-review"),
    ("merge", "merging"),
    ("nonsense", None),
])
def test_stage_status_mapping(stage, slug):
    assert bi.stage_status(stage) == slug


def test_announce_terminal_needs_attention(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()
    bi.announce_terminal(ledger, "22.4-002", "NEEDS_ATTENTION", runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any("gh issue comment 42" in c and "needs-attention" in c for c in joined)


def test_announce_terminal_done_is_noop(tmp_path):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="42")
    runner = FakeRunner()
    # DONE auto-closes via the merge's Closes #N — no separate terminal comment.
    bi.announce_terminal(ledger, "22.4-002", "DONE", runner=runner)
    assert runner.calls == []


# --- close-link is injected into the PR-opening prompts (AC1) ----------------


def _story() -> Story:
    return Story(
        id="22.4-002", title="Build-loop integration", epic_id="epic-22",
        epic_name="github-story-mirror", epic_file="docs/stories/epic-22.md",
        priority="Should", points=5, agent_type="python-backend-engineer",
    )


def test_build_prompt_includes_close_link_when_it_opens_the_pr():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=True)
    prompt = render_build_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" in prompt


def test_build_prompt_omits_close_link_when_coverage_opens_pr():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=False)
    # The build agent commits locally; the controller opens the PR after the
    # coverage gate (27.3-001), so no close-link here.
    prompt = render_build_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" not in prompt


def test_coverage_prompt_omits_close_link():
    # Story 27.3-001: the controller opens the CR itself and injects the
    # close-link into the CR body — the coverage agent never needs it.
    story = _story()
    opts = BuildOptions(scope="epic-22")
    prompt = render_coverage_prompt(story, opts, close_link="Closes #42")
    assert "Closes #42" not in prompt


def test_prompts_unchanged_without_close_link():
    story = _story()
    opts = BuildOptions(scope="epic-22", skip_coverage=True)
    assert "Closes #" not in render_build_prompt(story, opts)
    assert "Closes #" not in render_coverage_prompt(story, opts)


# --- change_request_checks (Story 25.1-001) ----------------------------------


def test_change_request_checks_unmapped_returns_none(tmp_path):
    """An unmapped story yields None so the merge re-check degrades to a no-op."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("25.1-001", "25", "25.1", "t", 5, "Should")])
    assert bi.change_request_checks(ledger, "25.1-001", 100, runner=FakeRunner()) is None


def test_change_request_checks_reads_github_view(tmp_path):
    import json

    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="25.1-001", host=ih.GITHUB, ref="42")
    payload = json.dumps({
        "labels": [{"name": "risk:high"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "High-risk file approval gate",
             "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    })
    runner = FakeRunner({"pr view": (0, payload, "")})
    view = bi.change_request_checks(ledger, "25.1-001", 100, runner=runner)
    assert view is not None
    assert view.labels == ("risk:high",)
    assert view.checks == (("High-risk file approval gate", ih.CR_FAILED),)
    # It queries the change request (PR #100), not the issue mapping ref.
    assert "100" in runner.calls[-1]


def test_change_request_checks_tolerates_host_failure(tmp_path):
    """A host error yields None — never raises — so a hiccup never parks a story."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="25.1-001", host=ih.GITHUB, ref="42")
    runner = FakeRunner(default=(1, "", "boom"))
    assert bi.change_request_checks(ledger, "25.1-001", 100, runner=runner) is None


# --- declared forge instance (Story 30.1-001) --------------------------------


def test_mirror_lifecycle_threads_declared_instance(tmp_path, monkeypatch):
    """Story 30.1-001: the inventory records the forge *kind*, so the repo's
    `.sdlc-forge.yaml` must supply the instance for the mirror-lifecycle
    adapters too. Without it every `glab` call below targets gitlab.com — the CI
    poll never resolves and the merge gate degrades — on exactly the local-forge
    repos the declaration exists for."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITLAB, ref="7")
    (tmp_path / ".sdlc-forge.yaml").write_text(
        "forge: gitlab\ngitlab_url: http://127.0.0.1:8080\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    seen: list = []

    def runner(argv, timeout=None, env=None):
        seen.append(env)
        return ih.RunResult(returncode=0, stdout="{}", stderr="")

    assert bi.close_link(ledger, "22.4-002", runner=runner) == "Closes #7"
    bi.change_request_status(ledger, "22.4-002", 9, runner=runner)

    assert seen, "no host call was made"
    assert all(env["GITLAB_HOST"] == "http://127.0.0.1:8080" for env in seen)


def test_mirror_lifecycle_ignores_declaration_for_another_forge(tmp_path, monkeypatch):
    """A declaration only contributes its instance to the forge it names — a
    GitHub-mapped story never inherits a GitLab instance."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITHUB, ref="7")
    (tmp_path / ".sdlc-forge.yaml").write_text(
        "forge: gitlab\ngitlab_url: http://127.0.0.1:8080\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    runner = FakeRunner()  # no `env` keyword: being passed one would raise
    assert bi.close_link(ledger, "22.4-002", runner=runner) == "Closes #7"


def test_mirror_lifecycle_no_declaration_is_unchanged(tmp_path, monkeypatch):
    """AC2: with no `.sdlc-forge.yaml` the runner is invoked exactly as before —
    a pre-existing double with no `env` keyword is never called with one."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITLAB, ref="7")
    monkeypatch.chdir(tmp_path)

    runner = FakeRunner()
    assert bi.close_link(ledger, "22.4-002", runner=runner) == "Closes #7"


def test_mirror_lifecycle_no_ops_on_malformed_declaration(tmp_path, monkeypatch):
    """A malformed declaration means the instance is unknown; these best-effort
    seams no-op rather than guess and talk to the wrong forge (a build already
    refused upstream at `build._forge_declaration_error`)."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITLAB, ref="7")
    (tmp_path / ".sdlc-forge.yaml").write_text("forge: bitbucket\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runner = FakeRunner()
    assert bi.close_link(ledger, "22.4-002", runner=runner) is None
    assert bi.change_request_status(ledger, "22.4-002", 9, runner=runner) is None
    assert bi.change_request_terms(ledger, "22.4-002", runner=runner) is ih.GITHUB_CR_TERMS
    assert runner.calls == []


# --- issue #677: the inventory mapping is a per-checkout cache ---------------
# `sdlc issues init` writes the story→issue mapping into *one* checkout's
# ledger. A build run from any other checkout (or a fresh clone) reads no row,
# so the close-link vanished from the PR and the merge CI gate silently skipped.
# The mapping is now recovered from the host by the story's body marker.


@pytest.fixture(autouse=True)
def _clear_recovery_cache():
    """The marker-recovery cache is process-wide; keep it from leaking across tests."""
    bi._RECOVERED_REFS.clear()
    yield
    bi._RECOVERED_REFS.clear()


def _mirror_repo(tmp_path, monkeypatch, forge=ih.GITHUB) -> Ledger:
    """A ledger that already mirrors *some* story, in a repo declaring ``forge``."""
    ledger = _ledger(tmp_path)
    # Evidence this repo uses the mirror: one other story is mapped.
    _mapped(ledger, story_id="22.1-001", host=forge, ref="1")
    (tmp_path / ".sdlc-forge.yaml").write_text(f"forge: {forge}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return ledger


def _found_payload(story_id: str, number: int) -> str:
    """A `gh issue list --json` row whose body carries the story's hidden marker."""
    import json

    from sdlc.story_render import story_marker

    return json.dumps([{
        "number": number, "url": f"https://example.test/{number}",
        "title": f"{story_id}: t", "state": "OPEN",
        "body": f"body\n{story_marker(story_id)}\n", "assignees": [],
    }])


def test_close_link_recovers_an_unmapped_story_by_marker(tmp_path, monkeypatch):
    """The regression: a story mirrored from another checkout still gets Closes #N."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    runner = FakeRunner({"issue list": (0, _found_payload("29.4-004", 573), "")})

    assert bi.close_link(ledger, "29.4-004", runner=runner) == "Closes #573"


def test_recovered_mapping_is_cached_in_the_inventory(tmp_path, monkeypatch):
    """A recovered mapping is written back so the next run in this checkout is local."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    runner = FakeRunner({"issue list": (0, _found_payload("29.4-004", 573), "")})

    bi.close_link(ledger, "29.4-004", runner=runner)

    assert ledger.inventory_get_mapping("29.4-004") == (ih.GITHUB, "573")


def test_marker_search_happens_once_per_story(tmp_path, monkeypatch):
    """Every mirror-lifecycle seam shares one host search, not one search each."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    runner = FakeRunner({"issue list": (0, _found_payload("29.4-004", 573), "")})

    bi.close_link(ledger, "29.4-004", runner=runner)
    bi.close_link(ledger, "29.4-004", runner=runner)
    bi.change_request_terms(ledger, "29.4-004", runner=runner)

    searches = [c for c in runner.calls if "list" in c]
    assert len(searches) == 1


def test_merge_ci_gate_resolves_status_for_an_unmapped_story(tmp_path, monkeypatch):
    """A missing *mirror* mapping must not disable the merge CI gate (issue #677).

    The CR number is already known at that point; the pipeline lookup only needs
    the repo's own forge. Here the marker search finds nothing at all, so even
    the recovery misses — the gate must still read the CR's pipeline.
    """
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("30.1-001", "30", "30.1", "t", 3, "Should")])
    runner = FakeRunner({
        "issue list": (0, "[]", ""),
        "pr view": (0, '{"statusCheckRollup": [{"status": "COMPLETED", '
                       '"conclusion": "SUCCESS"}]}', ""),
    })

    assert bi.change_request_status(ledger, "30.1-001", 672, runner=runner) == ih.CR_SUCCESS


def test_unmapped_story_logs_a_warn_event_naming_the_story(tmp_path, monkeypatch):
    """A genuine miss is surfaced, not silently downgraded to a debug log."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    run_id = ledger.run_create("epic-29", "build")
    runner = FakeRunner({"issue list": (0, "[]", "")})

    assert bi.close_link(ledger, "29.4-004", runner=runner, run_id=run_id) is None

    events = ledger.recent_events(run_id, limit=50)
    warns = [e for e in events if e["level"] == "warn" and "29.4-004" in e["message"]]
    assert warns, f"no warn event naming the story: {[e['message'] for e in events]}"


def test_no_host_search_when_the_repo_never_mirrored(tmp_path, monkeypatch):
    """A repo that never ran `sdlc issues init` keeps today's zero-host-call path."""
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    (tmp_path / ".sdlc-forge.yaml").write_text("forge: github\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner()

    assert bi.close_link(ledger, "29.4-004", runner=runner) is None
    assert bi.change_request_status(ledger, "29.4-004", 7, runner=runner) is None
    assert runner.calls == []


def test_recorded_but_unusable_host_never_falls_back(tmp_path, monkeypatch):
    """A story mapped to an unsupported host is not re-guessed onto another forge."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    ledger.inventory_set_mapping("29.4-004", "bitbucket", "9")
    runner = FakeRunner()

    assert bi.close_link(ledger, "29.4-004", runner=runner) is None
    assert bi.change_request_status(ledger, "29.4-004", 7, runner=runner) is None
    assert runner.calls == []


def test_recovery_tolerates_a_host_search_failure(tmp_path, monkeypatch):
    """A failing marker search degrades to today's no-op — it never raises."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    runner = FakeRunner(default=(1, "", "boom"))

    assert bi.close_link(ledger, "29.4-004", runner=runner) is None
    assert bi.change_request_terms(ledger, "29.4-004", runner=runner) is ih.GITHUB_CR_TERMS


def test_recovery_no_ops_on_a_malformed_declaration(tmp_path, monkeypatch):
    """An unresolvable forge means no search and no fallback adapter."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, story_id="22.1-001", host=ih.GITHUB, ref="1")
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    (tmp_path / ".sdlc-forge.yaml").write_text("forge: bitbucket\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner()

    assert bi.close_link(ledger, "29.4-004", runner=runner) is None
    assert bi.change_request_status(ledger, "29.4-004", 7, runner=runner) is None
    assert runner.calls == []


def test_recovered_story_gets_status_announcements(tmp_path, monkeypatch):
    """The whole mirror lifecycle recovers, not just the close-link."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    runner = FakeRunner({"issue list": (0, _found_payload("29.4-004", 573), "")})

    assert bi.announce_status(ledger, "29.4-004", "building", runner=runner) == "building"
    assert any("gh issue comment 573" in " ".join(c) for c in runner.calls)


def test_recovery_survives_a_story_with_no_inventory_row(tmp_path, monkeypatch):
    """A story never projected into *this* checkout still recovers (the reported case)."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    # No inventory_upsert_specs for this story at all: inventory_set_mapping is a
    # no-op, so only the in-process cache can keep the search down to one.
    runner = FakeRunner({"issue list": (0, _found_payload("32.1-001", 700), "")})

    assert bi.close_link(ledger, "32.1-001", runner=runner) == "Closes #700"
    assert bi.close_link(ledger, "32.1-001", runner=runner) == "Closes #700"
    assert len([c for c in runner.calls if "list" in c]) == 1


def test_warn_event_failure_never_breaks_the_lookup(tmp_path, monkeypatch):
    """A ledger hiccup while logging the warn degrades to a debug log, not a raise."""
    ledger = _mirror_repo(tmp_path, monkeypatch)
    ledger.inventory_upsert_specs([("29.4-004", "29", "29.4", "t", 3, "Should")])
    monkeypatch.setattr(
        ledger, "event_log",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger is locked")),
    )
    runner = FakeRunner({"issue list": (0, "[]", "")})

    assert bi.close_link(ledger, "29.4-004", runner=runner, run_id="run-1") is None
