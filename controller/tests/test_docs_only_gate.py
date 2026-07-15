# ABOUTME: Tests the docs-only gate skip in the build stage loop (Story 27.2-001).
# ABOUTME: Docs-only stories skip coverage + the adversarial slot; code stories run the full chain.

from __future__ import annotations

import sqlite3
from pathlib import Path

import sdlc.build as build_mod
import sdlc.change_class as change_class_mod
import sdlc.issue_host as issue_host_mod
from sdlc.build import BuildOptions, Ledger, run_build, status_snapshot
from sdlc.change_class import CODE, DOCS_ONLY
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult
from sdlc.issue_host import ChangeRequest

_PAYLOADS = {
    "build": {"branch_name": "feature/27.2-001", "build_status": "SUCCESS", "commit_sha": "a"},
    "coverage": {
        "pr_number": 200, "pr_url": "u", "coverage_pct": 95.0, "tests_added": 1,
        "coverage_status": "PASS", "security_status": "PASS",
    },
    "review": {"pr_number": 200, "approval_status": "APPROVED", "change_count": 0,
               "final_status": "APPROVED"},
    "merge": {"pr_number": 200, "merge_status": "MERGED", "merge_sha": "b",
              "merged_at": "2026-07-15T00:00:00Z"},
}


class _RecordingDispatcher:
    """Records each dispatch's (stage, prompt, kwargs) and returns a canned success."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.prompts: dict[str, str] = {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append((agent_type, kwargs))
        self.prompts[agent_type] = prompt
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw="")

    def stages(self) -> list[str]:
        return [a for a, _ in self.calls]


def _story() -> Story:
    return Story(
        id="27.2-001", title="t", epic_id="epic-27", epic_name="e",
        epic_file="f.md", priority="Must", points=2, agent_type="python",
    )


def _run(tmp_path, monkeypatch, *, files: list[str], cr: int | None = 100,
         harness_map: dict[str, str] | None = None):
    """Drive one story through run_build with a stubbed diff feed + CR opener."""
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda root, base, branch: list(files)
    )
    monkeypatch.setattr(
        build_mod, "_open_docs_only_cr",
        lambda story, ledger, run_id, workdir, base_ref, close_link, cr_terms: cr,
    )
    monkeypatch.setattr(build_mod, "_story_high_risk", lambda story, opts: False)
    disp = _RecordingDispatcher()
    ledger = Ledger(tmp_path / "ledger.db")
    opts = BuildOptions(
        scope="epic-27", skip_preflight=True, sequential=True,
        harness_map=dict(harness_map or {}),
    )
    run_build(
        opts, queue=[_story()], ledger=ledger, dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    return disp, ledger


def _stage_rows(tmp_path) -> list[tuple]:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    return conn.execute(
        "SELECT stage_name, status, failure_category, harness FROM stages "
        "ORDER BY rowid"
    ).fetchall()


def _events(tmp_path) -> list[str]:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    return [r[0] for r in conn.execute("SELECT message FROM events").fetchall()]


# ---------------------------------------------------------------------------
# AC1: a docs-only diff skips the coverage dispatch
# ---------------------------------------------------------------------------


def test_docs_only_story_skips_coverage_dispatch(tmp_path, monkeypatch) -> None:
    disp, _ = _run(tmp_path, monkeypatch, files=["README.md", "docs/guide.md"])
    assert "coverage" not in disp.stages()
    # Build, the (non-adversarial) review, and the merge all still run (AC4).
    assert disp.stages() == ["build", "review", "merge"]


def test_docs_only_skip_records_skip_reason_in_ledger(tmp_path, monkeypatch) -> None:
    _run(tmp_path, monkeypatch, files=["README.md"])
    rows = {name: (status, category) for name, status, category, _ in _stage_rows(tmp_path)}
    # AC2: recorded as SKIPPED with a skip_reason — never displayed as passed.
    assert rows["coverage"] == ("SKIPPED", "docs-only")
    assert any("skip_reason=docs-only" in m for m in _events(tmp_path))


def test_docs_only_skip_surfaces_skipped_in_status_snapshot(tmp_path, monkeypatch) -> None:
    _, ledger = _run(tmp_path, monkeypatch, files=["README.md"])
    snap = status_snapshot(ledger)
    stages = {s["name"]: s for s in snap["stories"][0]["stages"]}
    assert stages["coverage"]["status"] == "SKIPPED"
    assert stages["coverage"]["failure_category"] == "docs-only"


def test_docs_only_story_threads_controller_opened_pr_to_review(
    tmp_path, monkeypatch
) -> None:
    disp, _ = _run(tmp_path, monkeypatch, files=["README.md"], cr=100)
    # The review runs against the controller-opened CR, not a dangling None.
    assert "#100" in disp.prompts["review"]
    conn = sqlite3.connect(tmp_path / "ledger.db")
    status = conn.execute("SELECT status FROM stories").fetchone()[0]
    assert status == "DONE"


# ---------------------------------------------------------------------------
# AC3: any non-docs file → the full gate chain runs unchanged
# ---------------------------------------------------------------------------


def test_code_story_runs_full_gate_chain(tmp_path, monkeypatch) -> None:
    disp, _ = _run(tmp_path, monkeypatch, files=["README.md", "src/build.py"])
    assert disp.stages() == ["build", "coverage", "review", "merge"]
    assert all(status != "SKIPPED" for _, status, _, _ in _stage_rows(tmp_path))


def test_unclassifiable_empty_diff_runs_full_gate_chain(tmp_path, monkeypatch) -> None:
    # An empty/unreadable diff is conservatively `code`: never skip on absence
    # of evidence.
    disp, _ = _run(tmp_path, monkeypatch, files=[])
    assert disp.stages() == ["build", "coverage", "review", "merge"]


def test_docs_only_cr_open_failure_falls_back_to_coverage_dispatch(
    tmp_path, monkeypatch
) -> None:
    # The deterministic push/CR-open failing must never strand the story
    # without a change request — the full coverage dispatch runs instead.
    disp, _ = _run(tmp_path, monkeypatch, files=["README.md"], cr=None)
    assert disp.stages() == ["build", "coverage", "review", "merge"]
    assert all(status != "SKIPPED" for _, status, _, _ in _stage_rows(tmp_path))


# ---------------------------------------------------------------------------
# The adversarial slot: skipped for docs-only, kept for code (AC1/AC4)
# ---------------------------------------------------------------------------


def test_docs_only_story_skips_adversarial_review_slot(tmp_path, monkeypatch) -> None:
    disp, _ = _run(
        tmp_path, monkeypatch, files=["README.md"], harness_map={"review": "codex"}
    )
    review_kwargs = dict(disp.calls)["review"]
    # The review dispatch collapses to the built-in default harness — no Codex
    # adversarial argv is routed.
    assert review_kwargs.get("agent_cmd") is None
    rows = {name: harness for name, _, _, harness in _stage_rows(tmp_path)}
    assert rows["review"] == "claude"
    assert any("adversarial slot skipped" in m for m in _events(tmp_path))


def test_code_story_keeps_adversarial_review_slot(tmp_path, monkeypatch) -> None:
    disp, _ = _run(
        tmp_path, monkeypatch, files=["src/build.py"],
        harness_map={"review": "codex"},
    )
    review_kwargs = dict(disp.calls)["review"]
    assert review_kwargs.get("agent_cmd") is not None
    assert "codex-build-adapter.sh" in " ".join(review_kwargs["agent_cmd"])
    rows = {name: harness for name, _, _, harness in _stage_rows(tmp_path)}
    assert rows["review"] == "codex"
    assert not any("adversarial slot skipped" in m for m in _events(tmp_path))


# ---------------------------------------------------------------------------
# _review_is_adversarial_slot — which routings count as the adversarial slot
# ---------------------------------------------------------------------------


def test_review_slot_detection_against_checked_in_registries() -> None:
    is_slot = build_mod._review_is_adversarial_slot
    # The checked-in codex reviewer claims the codex harness (Story 20.3-002).
    assert is_slot(BuildOptions(harness_map={"review": "codex"})) is True
    # No routing / the built-in claude review is the standard pipeline review.
    assert is_slot(BuildOptions()) is False
    assert is_slot(BuildOptions(harness_map={"review": "claude"})) is False
    # A non-review role on codex does not make the review adversarial.
    assert is_slot(BuildOptions(harness_map={"build": "codex"})) is False
    # qwen is a harness but no reviewer claims it — not the adversarial slot.
    assert is_slot(BuildOptions(harness_map={"review": "qwen"})) is False


def test_review_slot_detection_degrades_to_false_on_registry_error(
    monkeypatch,
) -> None:
    # A broken registry must never fail a build: the slot check returns False
    # and the review dispatches exactly as routed.
    import sdlc.role_routing as role_routing_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(role_routing_mod, "resolve_role_routing", _boom)
    assert (
        build_mod._review_is_adversarial_slot(
            BuildOptions(harness_map={"review": "codex"})
        )
        is False
    )


# ---------------------------------------------------------------------------
# _story_change_class — classification wrapper (best-effort, evented)
# ---------------------------------------------------------------------------


class _EventLedger:
    def __init__(self) -> None:
        self.events: list[str] = []

    def event_log(self, run_id, story_id, level, source, message) -> None:
        self.events.append(message)


def test_story_change_class_logs_verdict(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda root, base, branch: ["README.md"]
    )
    ledger = _EventLedger()
    verdict = build_mod._story_change_class(
        _story(), ledger, "run-1", tmp_path, "origin/main"
    )
    assert verdict == DOCS_ONLY
    assert any("change class: docs-only" in m for m in ledger.events)


def test_story_change_class_malformed_allowlist_degrades_to_code(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        change_class_mod, "changed_files", lambda root, base, branch: ["README.md"]
    )
    (tmp_path / change_class_mod.OVERRIDE_FILENAME).write_text(
        "docs_patterns: 42\n", encoding="utf-8"
    )
    ledger = _EventLedger()
    verdict = build_mod._story_change_class(
        _story(), ledger, "run-1", tmp_path, "origin/main"
    )
    # A typo'd allowlist can only ever run MORE gates, never fewer.
    assert verdict == CODE
    assert any("change-class" in m for m in ledger.events)


# ---------------------------------------------------------------------------
# _open_docs_only_cr — deterministic push + CR open (with failure fallback)
# ---------------------------------------------------------------------------


def _fake_adapter(created: list[dict]):
    class _Adapter:
        def cr_create(self, source_branch, title, body, target_branch=None, draft=False):
            created.append({
                "source_branch": source_branch, "title": title, "body": body,
                "target_branch": target_branch,
            })
            return ChangeRequest(host="github", ref="123", url="https://x/pull/123")

    return _Adapter()


def _repo_with_origin(tmp_path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    root.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feature/27.2-001"], cwd=root, check=True, capture_output=True)
    return root


def test_open_docs_only_cr_pushes_and_opens(tmp_path, monkeypatch) -> None:
    from sdlc.issue_host import GITHUB_CR_TERMS

    root = _repo_with_origin(tmp_path)
    created: list[dict] = []
    monkeypatch.setattr(issue_host_mod, "resolve_host", lambda r, override=None: "github")
    monkeypatch.setattr(issue_host_mod, "get_adapter", lambda host, runner=None: _fake_adapter(created))
    ledger = _EventLedger()
    pr = build_mod._open_docs_only_cr(
        _story(), ledger, "run-1", root, "origin/main", "Closes #7", GITHUB_CR_TERMS
    )
    assert pr == 123
    assert created[0]["source_branch"] == "feature/27.2-001"
    assert created[0]["target_branch"] == "main"
    assert "Closes #7" in created[0]["body"]
    # The branch actually landed on the remote.
    import subprocess

    out = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", "feature/27.2-001"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    assert "feature/27.2-001" in out.stdout


def test_open_docs_only_cr_failure_returns_none_and_warns(tmp_path) -> None:
    # No origin remote → the push fails → None, and the fallback reason is
    # recorded for the run log.
    from sdlc.issue_host import GITHUB_CR_TERMS

    ledger = _EventLedger()
    pr = build_mod._open_docs_only_cr(
        _story(), ledger, "run-1", tmp_path, "origin/main", None, GITHUB_CR_TERMS
    )
    assert pr is None
    assert any("falling back" in m for m in ledger.events)
