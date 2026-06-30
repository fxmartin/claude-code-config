# ABOUTME: Tests for the merge CI-status gate — poll the CR pipeline, only merge on green.
# ABOUTME: Story 23.2-002 — poll-to-completion, fail-blocks-merge, pass-merges, no-CI degradation.

from __future__ import annotations

import pytest

from sdlc import build_issue as bi
from sdlc import issue_host as ih
from sdlc.build import (
    BuildOptions,
    Ledger,
    _evaluate_ci_gate,
    _GATE_BLOCK,
    _GATE_PASS,
    _GATE_SKIP,
    _poll_cr_status,
    _run_merge_ci_gate,
    parse_build_args,
)
from sdlc.cohort import Story


# --- a recording fake runner (same shape as test_build_issue) ----------------


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


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    return ledger


def _mapped(ledger: Ledger, story_id="23.2-002", host=ih.GITLAB, ref="42") -> None:
    ledger.inventory_upsert_specs([(story_id, "23", "23.2", "t", 5, "Should")])
    ledger.inventory_set_mapping(story_id, host, ref)


def _story(story_id="23.2-002") -> Story:
    return Story(
        id=story_id, title="Gate the merge on GitLab CI", epic_id="epic-23",
        epic_name="pipeline-on-gitlab", epic_file="docs/stories/epic-23.md",
        priority="Should", points=5, agent_type="python-backend-engineer",
    )


class _Clock:
    """A deterministic clock that advances by exactly the slept duration."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# --- _poll_cr_status ---------------------------------------------------------


def test_poll_returns_immediately_on_terminal_status():
    """A first read that is already terminal returns without sleeping (AC pass)."""
    clock = _Clock()
    calls = {"n": 0}

    def status_fn():
        calls["n"] += 1
        return ih.CR_SUCCESS

    status, polls, waited = _poll_cr_status(
        status_fn, timeout_s=1800, poll_s=30, sleep_fn=clock.sleep, clock=clock
    )
    assert status == ih.CR_SUCCESS
    assert polls == 1
    assert waited == 0.0
    assert calls["n"] == 1  # no extra polls once terminal


def test_poll_to_completion_polls_until_pipeline_finishes():
    """A running pipeline is polled until it flips to a terminal status (AC1)."""
    clock = _Clock()
    sequence = iter([ih.CR_PENDING, ih.CR_PENDING, ih.CR_SUCCESS])

    status, polls, waited = _poll_cr_status(
        lambda: next(sequence), timeout_s=1800, poll_s=30,
        sleep_fn=clock.sleep, clock=clock,
    )
    assert status == ih.CR_SUCCESS
    assert polls == 3
    assert waited == 60.0  # two 30s sleeps between the three reads


def test_poll_to_completion_observes_a_failure():
    """Polling resolves a pipeline that ends red (AC2 feeds the block)."""
    clock = _Clock()
    sequence = iter([ih.CR_PENDING, ih.CR_FAILED])
    status, polls, _ = _poll_cr_status(
        lambda: next(sequence), timeout_s=1800, poll_s=30,
        sleep_fn=clock.sleep, clock=clock,
    )
    assert status == ih.CR_FAILED
    assert polls == 2


def test_poll_times_out_while_still_pending():
    """A never-finishing pipeline returns CR_PENDING once the bounded timeout lapses (AC1)."""
    clock = _Clock()
    status, polls, waited = _poll_cr_status(
        lambda: ih.CR_PENDING, timeout_s=90, poll_s=30,
        sleep_fn=clock.sleep, clock=clock,
    )
    assert status == ih.CR_PENDING  # still pending → the gate treats this as a timeout
    assert waited >= 90
    assert polls >= 2


def test_poll_never_overshoots_the_deadline():
    """The final sleep is clamped so the wait never exceeds the timeout budget."""
    clock = _Clock()
    status, _, waited = _poll_cr_status(
        lambda: ih.CR_PENDING, timeout_s=50, poll_s=30,
        sleep_fn=clock.sleep, clock=clock,
    )
    assert status == ih.CR_PENDING
    assert waited == 50.0  # 30 + 20 (clamped), never 60


def test_poll_zero_timeout_reads_once():
    """A zero timeout degrades to a single status read with no wait."""
    clock = _Clock()
    status, polls, waited = _poll_cr_status(
        lambda: ih.CR_PENDING, timeout_s=0, poll_s=30,
        sleep_fn=clock.sleep, clock=clock,
    )
    assert status == ih.CR_PENDING
    assert polls == 1
    assert waited == 0.0


# --- _evaluate_ci_gate -------------------------------------------------------


def test_evaluate_success_passes():
    verdict, _ = _evaluate_ci_gate(ih.CR_SUCCESS, no_ci_policy="allow")
    assert verdict == _GATE_PASS


@pytest.mark.parametrize("status", [ih.CR_FAILED, ih.CR_UNKNOWN, ih.CR_PENDING])
def test_evaluate_non_green_blocks(status):
    """Failed/unknown/timeout-pending all block the merge (AC2)."""
    verdict, reason = _evaluate_ci_gate(status, no_ci_policy="allow")
    assert verdict == _GATE_BLOCK
    assert reason  # carries a human-readable cause


def test_evaluate_none_status_skips():
    """An unresolvable CI source (unmapped/host error) degrades to a no-op skip."""
    verdict, _ = _evaluate_ci_gate(None, no_ci_policy="allow")
    assert verdict == _GATE_SKIP


def test_evaluate_no_ci_allows_by_default():
    """No CI configured → allow-policy passes with a warning (AC4)."""
    verdict, reason = _evaluate_ci_gate(ih.CR_NONE, no_ci_policy="allow")
    assert verdict == _GATE_PASS
    assert "no ci" in reason.lower()


def test_evaluate_no_ci_can_deny():
    """No CI configured → deny-policy blocks (AC4 configurable allow/deny)."""
    verdict, _ = _evaluate_ci_gate(ih.CR_NONE, no_ci_policy="deny")
    assert verdict == _GATE_BLOCK


# --- _run_merge_ci_gate (orchestration) --------------------------------------


def test_gate_is_noop_for_non_merge_stage(tmp_path):
    ledger = _ledger(tmp_path)
    assert _run_merge_ci_gate(
        "build", ledger, "run", _story(), 100, BuildOptions(),
        status_fn=lambda: ih.CR_FAILED,
    ) is None


def test_gate_is_noop_without_a_cr_ref(tmp_path):
    ledger = _ledger(tmp_path)
    assert _run_merge_ci_gate(
        "merge", ledger, "run", _story(), None, BuildOptions(),
        status_fn=lambda: ih.CR_FAILED,
    ) is None


def test_gate_passes_on_green(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    clock = _Clock()
    gate = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(),
        status_fn=lambda: ih.CR_SUCCESS, sleep_fn=clock.sleep, clock=clock,
    )
    assert gate is not None
    assert gate.verdict == _GATE_PASS
    assert gate.status == ih.CR_SUCCESS


def test_gate_blocks_on_red(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    clock = _Clock()
    gate = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(),
        status_fn=lambda: ih.CR_FAILED, sleep_fn=clock.sleep, clock=clock,
    )
    assert gate is not None
    assert gate.verdict == _GATE_BLOCK
    assert gate.status == ih.CR_FAILED


def test_gate_polls_a_running_pipeline_then_merges(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    clock = _Clock()
    sequence = iter([ih.CR_PENDING, ih.CR_SUCCESS])
    gate = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(),
        status_fn=lambda: next(sequence), sleep_fn=clock.sleep, clock=clock,
    )
    assert gate.verdict == _GATE_PASS
    assert gate.polls == 2


def test_gate_no_ci_allow_vs_deny(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    clock = _Clock()
    allow = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(ci_gate_no_ci="allow"),
        status_fn=lambda: ih.CR_NONE, sleep_fn=clock.sleep, clock=clock,
    )
    deny = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(ci_gate_no_ci="deny"),
        status_fn=lambda: ih.CR_NONE, sleep_fn=clock.sleep, clock=clock,
    )
    assert allow.verdict == _GATE_PASS
    assert deny.verdict == _GATE_BLOCK


def test_gate_resolves_status_via_build_issue_by_default(tmp_path):
    """With no injected status_fn the gate polls the host adapter via build_issue."""
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    _mapped(ledger, host=ih.GITLAB, ref="42")
    # The GitLab MR pipeline reads green.
    runner = FakeRunner({"mr view": (0, '{"pipeline": {"status": "success"}}', "")})

    def status_fn():
        return bi.change_request_status(ledger, "23.2-002", 7, runner=runner)

    gate = _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 7, BuildOptions(), status_fn=status_fn,
    )
    assert gate.verdict == _GATE_PASS
    # It queried the MR pipeline, not the issue.
    assert any("mr view 7" in " ".join(c) for c in runner.calls)


def test_gate_logs_the_outcome_to_the_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = ledger.run_create("epic-23", "build")
    clock = _Clock()
    _run_merge_ci_gate(
        "merge", ledger, run_id, _story(), 100, BuildOptions(),
        status_fn=lambda: ih.CR_FAILED, sleep_fn=clock.sleep, clock=clock,
    )
    events = ledger.recent_events(run_id, limit=50)
    assert any("CI gate" in e["message"] for e in events)


# --- build_issue.change_request_status (best-effort adapter resolution) ------


@pytest.mark.parametrize("host,view,payload,expected", [
    (ih.GITLAB, "mr view", '{"pipeline": {"status": "success"}}', ih.CR_SUCCESS),
    (ih.GITLAB, "mr view", '{"pipeline": {"status": "failed"}}', ih.CR_FAILED),
    (ih.GITLAB, "mr view", '{"pipeline": {"status": "running"}}', ih.CR_PENDING),
    (ih.GITHUB, "pr view",
     '{"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}',
     ih.CR_SUCCESS),
    (ih.GITHUB, "pr view",
     '{"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}',
     ih.CR_FAILED),
])
def test_change_request_status_maps_each_host(tmp_path, host, view, payload, expected):
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=host, ref="42")
    runner = FakeRunner({view: (0, payload, "")})
    assert bi.change_request_status(ledger, "23.2-002", 7, runner=runner) == expected


def test_change_request_status_unmapped_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("23.2-002", "23", "23.2", "t", 5, "Should")])
    assert bi.change_request_status(ledger, "23.2-002", 7) is None


def test_change_request_status_unsupported_host_is_none(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.inventory_upsert_specs([("23.2-002", "23", "23.2", "t", 5, "Should")])
    ledger.inventory_set_mapping("23.2-002", "bitbucket", "9")
    assert bi.change_request_status(ledger, "23.2-002", 7) is None


def test_change_request_status_tolerates_host_failure(tmp_path):
    """A host CLI error must not raise — it degrades to None so the gate skips."""
    ledger = _ledger(tmp_path)
    _mapped(ledger, host=ih.GITLAB, ref="42")
    runner = FakeRunner(default=(1, "", "boom"))
    assert bi.change_request_status(ledger, "23.2-002", 7, runner=runner) is None


def test_change_request_status_tolerates_broken_ledger():
    class _NoInventory:
        pass

    assert bi.change_request_status(_NoInventory(), "23.2-002", 7) is None  # type: ignore[arg-type]


# --- CLI parsing of the gate knobs -------------------------------------------


def test_parse_ci_gate_flags():
    opts = parse_build_args([
        "epic-23", "--ci-gate-timeout=600", "--ci-gate-poll=15", "--ci-gate-no-ci=deny",
    ])
    assert opts.ci_gate_timeout_s == 600
    assert opts.ci_gate_poll_s == 15
    assert opts.ci_gate_no_ci == "deny"


def test_parse_ci_gate_defaults():
    opts = parse_build_args(["epic-23"])
    assert opts.ci_gate_timeout_s == 1800
    assert opts.ci_gate_poll_s == 30
    assert opts.ci_gate_no_ci == "allow"


def test_parse_ci_gate_rejects_bad_policy():
    with pytest.raises(ValueError):
        parse_build_args(["--ci-gate-no-ci=maybe"])


def test_parse_ci_gate_rejects_negative_timeout():
    with pytest.raises(ValueError):
        parse_build_args(["--ci-gate-timeout=-1"])
