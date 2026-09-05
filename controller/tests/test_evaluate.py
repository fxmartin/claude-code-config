# ABOUTME: Tests for the reproducible agentic eval harness (Story 18.1-001).
# ABOUTME: Diff scoring, usage/cost extraction, aggregation, config load, isolation runner.

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sdlc.dispatch import AgentResult, RateLimitError
from sdlc.evaluate import (
    DiffStats,
    EvalConfig,
    EvalConfigError,
    Provenance,
    RunResult,
    Ticket,
    aggregate,
    build_provenance,
    dispatcher_for_harness,
    host_identifier,
    load_config,
    parse_diff_numstat,
    render_table,
    resolve_eval_harness,
    result_cost,
    run_eval,
    run_quality_check,
    run_ticket,
    scoreboard_to_dict,
    tokens_from_usage,
    utc_timestamp,
)
from sdlc.harness import DEFAULT_HARNESS
from sdlc.rate_limit import RateLimitSignal


# ---------------------------------------------------------------------------
# parse_diff_numstat — LOC delta from git diff --numstat
# ---------------------------------------------------------------------------


def test_parse_numstat_counts_lines_and_files() -> None:
    stats = parse_diff_numstat("3\t1\ta.py\n10\t0\tb.py\n")
    assert stats == DiffStats(added=13, removed=1, files=2)
    assert stats.net == 12


def test_parse_numstat_binary_file_counts_as_touched_zero_lines() -> None:
    stats = parse_diff_numstat("-\t-\timage.png\n5\t2\tcode.py\n")
    assert stats == DiffStats(added=5, removed=2, files=2)


def test_parse_numstat_empty_is_zero() -> None:
    assert parse_diff_numstat("") == DiffStats(added=0, removed=0, files=0)


def test_parse_numstat_ignores_blank_and_malformed_lines() -> None:
    stats = parse_diff_numstat("\n2\t2\tok.py\ngarbage\n")
    assert stats == DiffStats(added=2, removed=2, files=1)


# ---------------------------------------------------------------------------
# tokens_from_usage — sum the four envelope keys
# ---------------------------------------------------------------------------


def test_tokens_sums_all_four_components() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 5,
    }
    assert tokens_from_usage(usage) == 165


def test_tokens_treats_missing_keys_as_zero() -> None:
    assert tokens_from_usage({"input_tokens": 7}) == 7


def test_tokens_none_when_no_usage() -> None:
    assert tokens_from_usage(None) is None
    assert tokens_from_usage({}) is None
    assert tokens_from_usage({"input_tokens": None}) is None


# ---------------------------------------------------------------------------
# result_cost — real cost or notional fallback
# ---------------------------------------------------------------------------


def _result(usage: dict | None = None, cost: float | None = None) -> AgentResult:
    return AgentResult(agent_type="build", data={}, raw="", usage=usage, cost_usd=cost)


def test_cost_uses_envelope_when_present() -> None:
    assert result_cost(_result(cost=0.42)) == 0.42


def test_cost_falls_back_to_notional_from_tokens() -> None:
    # 1,000,000 tokens at the $15/Mtok notional convention => $15.
    cost = result_cost(
        _result(usage={"input_tokens": 1_000_000}),
        usd_per_million_tokens=15.0,
    )
    assert cost == pytest.approx(15.0)


def test_cost_none_when_no_cost_and_no_tokens() -> None:
    assert result_cost(_result()) is None


# ---------------------------------------------------------------------------
# Story 31.2-003 — cost provenance: metered (hosted, unchanged) vs not-metered
# (local inference) vs a configured local rate.
# ---------------------------------------------------------------------------


def test_cost_from_hosted_metered_envelope_unchanged() -> None:
    """AC1: a metered (hosted) harness's own reported cost is used as-is."""
    from sdlc.evaluate import MEASURED, _cost_from
    from sdlc.usage import UNAVAILABLE_USAGE

    cost, source = _cost_from(
        0.42, UNAVAILABLE_USAGE, usd_per_million_tokens=15.0, metered=True
    )
    assert cost == 0.42
    assert source == MEASURED


def test_cost_from_hosted_metered_falls_back_to_notional() -> None:
    """AC1: hosted fallback-to-notional path is unchanged."""
    from sdlc.evaluate import ESTIMATED, _cost_from
    from sdlc.usage import TokenBreakdown

    usage = TokenBreakdown(input_tokens=1_000_000)
    cost, source = _cost_from(
        None, usage, usd_per_million_tokens=15.0, metered=True
    )
    assert cost == pytest.approx(15.0)
    assert source == ESTIMATED


def test_cost_from_not_metered_ignores_literal_zero_telemetry() -> None:
    """AC2 (field case): a local harness's own `cost: 0` is not a real $0 —
    it must not be trusted at face value, and stays distinct from a genuinely
    missing figure."""
    from sdlc.evaluate import NOT_METERED, _cost_from
    from sdlc.usage import TokenBreakdown

    usage = TokenBreakdown(input_tokens=1000, output_tokens=200)
    cost, source = _cost_from(0.0, usage, usd_per_million_tokens=15.0, metered=False)
    assert cost is None
    assert source == NOT_METERED


def test_cost_from_not_metered_no_tokens_is_unavailable_not_not_metered() -> None:
    """A run with no tokens at all is `unavailable`, distinct from a
    deliberate `not_metered` harness state — the two `None`s must not collapse."""
    from sdlc.evaluate import _cost_from
    from sdlc.usage import UNAVAILABLE, UNAVAILABLE_USAGE

    cost, source = _cost_from(
        None,
        UNAVAILABLE_USAGE,
        usd_per_million_tokens=15.0,
        metered=False,
        local_rate_usd_per_million_tokens=2.5,
    )
    assert cost is None
    assert source == UNAVAILABLE


def test_cost_from_not_metered_with_configured_local_rate() -> None:
    """AC3/AC5: an explicit local rate derives a real figure from tokens."""
    from sdlc.evaluate import LOCAL_RATE, _cost_from
    from sdlc.usage import TokenBreakdown

    usage = TokenBreakdown(input_tokens=1_000_000)
    cost, source = _cost_from(
        None,
        usage,
        usd_per_million_tokens=15.0,
        metered=False,
        local_rate_usd_per_million_tokens=2.5,
    )
    assert cost == pytest.approx(2.5)
    assert source == LOCAL_RATE


def test_cost_from_not_metered_without_local_rate_is_none() -> None:
    """AC3: no configured rate leaves cost `None`, labelled `not_metered`."""
    from sdlc.evaluate import NOT_METERED, _cost_from
    from sdlc.usage import TokenBreakdown

    usage = TokenBreakdown(input_tokens=1_000_000)
    cost, source = _cost_from(None, usage, usd_per_million_tokens=15.0, metered=False)
    assert cost is None
    assert source == NOT_METERED


def test_result_cost_threads_metered_and_local_rate() -> None:
    cost = result_cost(
        _result(usage={"input_tokens": 1_000_000}, cost=0.0),
        usd_per_million_tokens=15.0,
        metered=False,
        local_rate_usd_per_million_tokens=1.0,
    )
    assert cost == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# run_quality_check — exit-code based pass/fail
# ---------------------------------------------------------------------------


def test_quality_check_none_when_no_command(tmp_path: Path) -> None:
    assert run_quality_check(None, tmp_path) is None


def test_quality_check_passes_on_exit_zero(tmp_path: Path) -> None:
    assert run_quality_check(["true"], tmp_path) is True


def test_quality_check_fails_on_nonzero(tmp_path: Path) -> None:
    assert run_quality_check(["false"], tmp_path) is False


def test_quality_check_missing_binary_is_failure_not_raise(tmp_path: Path) -> None:
    assert run_quality_check(["definitely-not-a-real-binary-xyz"], tmp_path) is False


# ---------------------------------------------------------------------------
# aggregate — per-ticket and overall means
# ---------------------------------------------------------------------------


def _run(
    ticket: str,
    idx: int,
    *,
    added: int = 0,
    removed: int = 0,
    tokens: int | None = None,
    cost: float | None = None,
    wall: float = 1.0,
    quality: bool | None = None,
    error: str | None = None,
    stall: float | None = None,
) -> RunResult:
    return RunResult(
        ticket_id=ticket,
        run_index=idx,
        diff=DiffStats(added=added, removed=removed, files=1),
        wall_s=wall,
        tokens=tokens,
        cost_usd=cost,
        quality_pass=quality,
        error=error,
        stall_s=stall,
    )


def test_aggregate_means_per_ticket() -> None:
    results = [
        _run("t1", 0, added=10, removed=2, tokens=100, cost=1.0, quality=True),
        _run("t1", 1, added=20, removed=4, tokens=200, cost=3.0, quality=False),
    ]
    board = aggregate(results, "demo")
    score = board.tickets[0]
    assert score.ticket_id == "t1"
    assert score.runs == 2
    assert score.loc_added_mean == 15.0
    assert score.loc_net_mean == 12.0  # ((10-2)+(20-4))/2
    assert score.tokens_mean == 150.0
    assert score.cost_mean == 2.0
    assert score.quality_pass_rate == 0.5


def test_aggregate_overall_spans_all_runs() -> None:
    results = [
        _run("t1", 0, added=10, quality=True),
        _run("t2", 0, added=30, quality=True),
    ]
    board = aggregate(results, "demo")
    assert board.overall is not None
    assert board.overall.ticket_id == "OVERALL"
    assert board.overall.runs == 2
    assert board.overall.loc_added_mean == 20.0
    assert board.overall.quality_pass_rate == 1.0


def test_aggregate_optional_means_none_when_all_absent() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo")
    score = board.tickets[0]
    assert score.tokens_mean is None
    assert score.cost_mean is None
    assert score.quality_pass_rate is None


def test_aggregate_counts_errors_and_ignores_missing_quality() -> None:
    results = [
        _run("t1", 0, quality=True),
        _run("t1", 1, error="boom"),  # no quality signal
    ]
    score = aggregate(results, "demo").tickets[0]
    assert score.errors == 1
    assert score.quality_pass_rate == 1.0  # only the one graded run counts


def test_aggregate_stall_mean_ignores_never_stalled_runs() -> None:
    # Story 31.2-001: a ticket where only one of two runs stalled reports the
    # mean over the runs that actually stalled — the None-not-zero convention
    # mirrors tokens_mean/cost_mean, so a never-stalled run never drags the
    # mean toward zero.
    results = [
        _run("t1", 0, wall=10.0, stall=None),
        _run("t1", 1, wall=8.0, stall=120.0),
    ]
    score = aggregate(results, "demo").tickets[0]
    assert score.stall_mean == 120.0
    assert score.wall_mean == 9.0


def test_aggregate_stall_mean_none_when_no_run_stalled() -> None:
    board = aggregate([_run("t1", 0, wall=5.0)], "demo")
    assert board.tickets[0].stall_mean is None


def test_aggregate_empty_has_no_overall() -> None:
    board = aggregate([], "demo")
    assert board.tickets == []
    assert board.overall is None


# ---------------------------------------------------------------------------
# render / serialise
# ---------------------------------------------------------------------------


def test_render_table_includes_tickets_and_overall() -> None:
    board = aggregate([_run("t1", 0, added=5, tokens=10, cost=1.0, quality=True)], "demo")
    table = render_table(board)
    assert "eval: demo" in table
    assert "t1" in table
    assert "OVERALL" in table


def test_render_table_shows_stalled_column() -> None:
    # Story 31.2-001: the stalled amount renders beside wall_s — a "—" for a
    # ticket that never stalled, the seconds for one that did.
    board = aggregate(
        [_run("t1", 0, wall=8.0, stall=120.0), _run("t2", 0, wall=5.0)], "demo"
    )
    table = render_table(board)
    assert "stalled" in table
    lines = {line.split()[0]: line for line in table.splitlines()}
    assert "120.0" in lines["t1"]
    assert "—" in lines["t2"]


def test_scoreboard_to_dict_roundtrips_shape() -> None:
    board = aggregate([_run("t1", 0, added=5, tokens=10, cost=1.0, quality=True)], "demo")
    payload = scoreboard_to_dict(board)
    assert payload["config_name"] == "demo"
    assert payload["tickets"][0]["ticket_id"] == "t1"
    assert payload["overall"]["runs"] == 1


# ---------------------------------------------------------------------------
# load_config — versioned YAML config
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    (tmp_path / "target").mkdir()
    path = tmp_path / "eval.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_parses_full_config(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
name: demo-eval
target: target
n: 3
seed: 42
agent_type: build
tickets:
  - id: t1
    prompt: do the thing
    quality_cmd: ["pytest", "-q"]
  - id: t2
    prompt: do another thing
""",
    )
    config = load_config(path)
    assert config.name == "demo-eval"
    assert config.n == 3
    assert config.seed == 42
    assert config.target == (tmp_path / "target").resolve()
    assert len(config.tickets) == 2
    assert config.tickets[0].quality_cmd == ["pytest", "-q"]
    assert config.tickets[1].quality_cmd is None


def test_load_config_defaults(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n  - id: t1\n    prompt: p\n",
    )
    config = load_config(path)
    assert config.n == 1
    assert config.seed is None
    assert config.agent_type == "build"


@pytest.mark.parametrize(
    "body",
    [
        "target: target\ntickets:\n  - id: t1\n    prompt: p\n",  # no name
        "name: d\ntickets:\n  - id: t1\n    prompt: p\n",  # no target
        "name: d\ntarget: target\n",  # no tickets
        "name: d\ntarget: target\ntickets: []\n",  # empty tickets
        "name: d\ntarget: target\nn: 0\ntickets:\n  - id: t1\n    prompt: p\n",  # n<1
        "name: d\ntarget: target\ntickets:\n  - prompt: p\n",  # ticket no id
        "name: d\ntarget: target\ntickets:\n  - id: t1\n",  # ticket no prompt
    ],
)
def test_load_config_rejects_invalid(tmp_path: Path, body: str) -> None:
    path = _write_config(tmp_path, body)
    with pytest.raises(EvalConfigError):
        load_config(path)


def test_load_config_rejects_duplicate_ticket_ids(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n"
        "  - id: t1\n    prompt: a\n"
        "  - id: t1\n    prompt: b\n",
    )
    with pytest.raises(EvalConfigError, match="duplicate"):
        load_config(path)


def test_load_config_missing_file_raises() -> None:
    with pytest.raises(EvalConfigError, match="not found"):
        load_config(Path("/no/such/eval.yaml"))


def test_load_config_rejects_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text("name: [unterminated\n", encoding="utf-8")
    with pytest.raises(EvalConfigError, match="invalid YAML"):
        load_config(path)


def test_load_config_rejects_non_mapping_root(tmp_path: Path) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(EvalConfigError, match="must be a mapping"):
        load_config(path)


def test_load_config_rejects_bad_seed_type(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nseed: not-an-int\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="seed"):
        load_config(path)


def test_load_config_rejects_empty_agent_type(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nagent_type: ''\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="agent_type"):
        load_config(path)


def test_load_config_rejects_non_mapping_ticket(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n  - just-a-string\n",
    )
    with pytest.raises(EvalConfigError, match="must be a mapping"):
        load_config(path)


def test_load_config_rejects_bad_quality_cmd(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n"
        "  - id: t1\n    prompt: p\n    quality_cmd: not-a-list\n",
    )
    with pytest.raises(EvalConfigError, match="quality_cmd"):
        load_config(path)


# ---------------------------------------------------------------------------
# run_eval — the isolation runner (fake dispatcher, real git)
# ---------------------------------------------------------------------------


def _sample_target(tmp_path: Path) -> Path:
    target = tmp_path / "sample"
    target.mkdir()
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return target


def test_run_eval_scores_diff_tokens_cost_and_quality(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="add-sub", prompt="add subtract", quality_cmd=["true"])],
    )

    def fake_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        # Edit the throwaway workspace, NOT the template.
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        return AgentResult(
            agent_type=agent_type,
            data={},
            raw="",
            usage={"input_tokens": 1000, "output_tokens": 200},
            cost_usd=0.05,
        )

    results = run_eval(config, tmp_path / "ws", dispatcher=fake_dispatcher)
    assert len(results) == 1
    run = results[0]
    assert run.ticket_id == "add-sub"
    assert run.diff.added == 4  # four new lines (2 blank + def + return)
    assert run.diff.removed == 0
    assert run.tokens == 1200
    assert run.cost_usd == 0.05
    assert run.quality_pass is True
    assert run.error is None
    assert run.wall_s >= 0.0


def test_run_eval_does_not_mutate_template(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    original = (target / "calc.py").read_text(encoding="utf-8")
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="edit")],
    )

    def fake_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        (cwd / "calc.py").write_text("mutated\n", encoding="utf-8")
        return AgentResult(agent_type=agent_type, data={}, raw="")

    run_eval(config, tmp_path / "ws", dispatcher=fake_dispatcher)
    # The versioned sample target is untouched — eval ran in isolation.
    assert (target / "calc.py").read_text(encoding="utf-8") == original
    assert not (target / ".git").exists()


def test_run_eval_captures_dispatch_failure_as_error(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="boom")],
    )

    def failing_dispatcher(*_a: object, **_k: object) -> AgentResult:
        raise RuntimeError("agent exploded")

    results = run_eval(config, tmp_path / "ws", dispatcher=failing_dispatcher)
    assert results[0].error is not None
    assert "agent exploded" in results[0].error
    assert results[0].diff.net == 0  # no edits applied


# ---------------------------------------------------------------------------
# Story 31.2-001 — a rate-limit hit mid-ticket is a recoverable pause: the
# ticket waits in-process and retries, and the wait is excluded from wall_s.
# ---------------------------------------------------------------------------


def test_run_ticket_retries_after_rate_limit_and_excludes_wait(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(name="demo", target=target, n=1, tickets=[Ticket(id="t1", prompt="p")])
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError(
                "429", signal=RateLimitSignal(source="429", retry_after_s=90)
            )
        return AgentResult(agent_type=agent_type, data={}, raw="")

    run = run_ticket(
        Ticket(id="t1", prompt="p"), config, 0, tmp_path / "ws",
        dispatcher=flaky_dispatcher, sleep_fn=sleeps.append,
    )
    assert run.status == "ok"
    assert run.error is None
    assert calls["n"] == 2  # the same dispatch was retried, not abandoned
    assert sleeps == [90]
    assert run.stall_s == 90.0
    assert run.wall_s >= 0.0


def test_run_ticket_accumulates_stall_across_multiple_waits(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(name="demo", target=target, n=1, tickets=[Ticket(id="t1", prompt="p")])
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RateLimitError(
                "429", signal=RateLimitSignal(source="429", retry_after_s=30)
            )
        return AgentResult(agent_type=agent_type, data={}, raw="")

    run = run_ticket(
        Ticket(id="t1", prompt="p"), config, 0, tmp_path / "ws",
        dispatcher=flaky_dispatcher, sleep_fn=sleeps.append,
    )
    assert run.status == "ok"
    assert sleeps == [30, 30]
    assert run.stall_s == 60.0  # a single throttled ticket's total wait, not per-hit


def test_run_ticket_gives_up_beyond_rate_limit_wait_cap(tmp_path: Path) -> None:
    # An eval sweep has no durable-park/resume path like a build run does — a
    # wait beyond the cap scores the ticket as an error instead of holding.
    target = _sample_target(tmp_path)
    config = EvalConfig(name="demo", target=target, n=1, tickets=[Ticket(id="t1", prompt="p")])

    def throttled_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        raise RateLimitError(
            "usage limit reached", signal=RateLimitSignal(source="usage-limit", retry_after_s=99_999)
        )

    run = run_ticket(
        Ticket(id="t1", prompt="p"), config, 0, tmp_path / "ws",
        dispatcher=throttled_dispatcher, sleep_fn=lambda _s: None,
        rate_limit_max_wait_s=100,
    )
    assert run.status == "error"
    assert run.stall_s is None  # nothing was actually waited
    assert run.error is not None and "rate limit" in run.error.lower()


def test_run_eval_runs_each_ticket_n_times(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=2,
        tickets=[Ticket(id="t1", prompt="a"), Ticket(id="t2", prompt="b")],
    )

    def noop_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        return AgentResult(agent_type=agent_type, data={}, raw="")

    results = run_eval(config, tmp_path / "ws", dispatcher=noop_dispatcher)
    assert len(results) == 4  # 2 tickets × 2 runs
    board = aggregate(results, config.name)
    assert {t.ticket_id for t in board.tickets} == {"t1", "t2"}
    assert all(t.runs == 2 for t in board.tickets)


# ---------------------------------------------------------------------------
# The shipped, versioned config is loadable (reproducibility provenance)
# ---------------------------------------------------------------------------


def test_shipped_eval_config_is_valid() -> None:
    config_path = Path(__file__).resolve().parents[1] / "eval" / "eval-config.yaml"
    config = load_config(config_path)
    assert config.tickets
    assert config.target.exists()
    assert config.seed is not None  # reproducibility provenance is versioned


# ---------------------------------------------------------------------------
# Issue #435 — live runs must not fail contract validation and lose metrics.
# The eval prompt carries a result-block contract, a concrete model is threaded,
# and a contract miss still records tokens/cost/quality (status=contract_miss).
# ---------------------------------------------------------------------------

from sdlc.contracts import (  # noqa: E402 — grouped with the issue-#435 tests
    RESULT_START_MARKER,
    ResultBlockError,
)
from sdlc.model_routing import BALANCED, select_model  # noqa: E402


def _contract_miss(usage: dict | None, cost: float | None) -> ResultBlockError:
    """A ContractError carrying the telemetry parsers.py attaches on a miss."""
    exc = ResultBlockError("agent ended with prose, no result block")
    exc.usage = usage
    exc.cost_usd = cost
    exc.usage_available = usage is not None
    return exc


def test_run_ticket_contract_miss_captures_tokens_cost_and_quality(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="edit", quality_cmd=["true"])],
    )

    def miss_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        # The agent did edit the workspace but ended with prose — a real diff,
        # real usage, but a failed contract. Metrics must survive.
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
        raise _contract_miss({"input_tokens": 1000, "output_tokens": 200}, 0.05)

    run = run_eval(config, tmp_path / "ws", dispatcher=miss_dispatcher)[0]
    assert run.status == "contract_miss"
    assert run.error is None  # a contract miss is scored, not discarded as error
    assert run.tokens == 1200
    assert run.cost_usd == 0.05
    assert run.quality_pass is True  # the quality command still ran
    assert run.diff.removed >= 1  # the agent's real edit was measured


def test_run_ticket_contract_miss_notional_cost_from_usage(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        usd_per_million_tokens=15.0,
        tickets=[Ticket(id="t1", prompt="edit")],
    )

    def miss_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        raise _contract_miss({"input_tokens": 1_000_000}, None)  # no envelope cost

    run = run_eval(config, tmp_path / "ws", dispatcher=miss_dispatcher)[0]
    assert run.status == "contract_miss"
    assert run.cost_usd == pytest.approx(15.0)  # notional fallback from tokens


def test_run_ticket_contract_miss_zero_usage_is_none_safe(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="edit")],
    )

    def miss_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        raise _contract_miss(None, None)  # a miss with no telemetry at all

    run = run_eval(config, tmp_path / "ws", dispatcher=miss_dispatcher)[0]
    assert run.status == "contract_miss"
    assert run.tokens is None
    assert run.cost_usd is None


def test_run_ticket_contract_miss_failing_quality_stays_contract_miss(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="edit", quality_cmd=["false"])],
    )

    def miss_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        raise _contract_miss({"input_tokens": 10}, 0.01)

    run = run_eval(config, tmp_path / "ws", dispatcher=miss_dispatcher)[0]
    assert run.status == "contract_miss"  # a failing quality check never masks the miss
    assert run.quality_pass is False


def test_run_ticket_non_contract_exception_is_still_error(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        tickets=[Ticket(id="t1", prompt="edit")],
    )

    def boom_dispatcher(*_a: object, **_k: object) -> AgentResult:
        raise RuntimeError("infrastructure failure")

    run = run_eval(config, tmp_path / "ws", dispatcher=boom_dispatcher)[0]
    assert run.status == "error"
    assert run.error is not None and "infrastructure failure" in run.error


def test_eval_prompt_carries_result_block_contract(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        agent_type="build",
        tickets=[Ticket(id="t1", prompt="add a function")],
    )
    seen: dict[str, str] = {}

    def capturing_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        seen["prompt"] = prompt
        return AgentResult(agent_type=agent_type, data={}, raw="")

    run_eval(config, tmp_path / "ws", dispatcher=capturing_dispatcher)
    # The bare ticket prompt is now wrapped with the schema-derived result block
    # so a live agent knows to emit the contract instead of ending with prose.
    assert "add a function" in seen["prompt"]
    assert RESULT_START_MARKER in seen["prompt"]
    assert "branch_name" in seen["prompt"]  # a build-schema required field


def test_eval_config_model_defaults_to_balanced_routing() -> None:
    config = EvalConfig(name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    # A concrete, pinned model — not None — so evals never silently run on the
    # user's current default model (issue #435).
    assert config.model is not None
    assert config.model == select_model("build", BALANCED)


def test_run_ticket_threads_model_to_dispatcher(tmp_path: Path) -> None:
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo",
        target=target,
        n=1,
        model="haiku",
        tickets=[Ticket(id="t1", prompt="p")],
    )
    seen: dict[str, object] = {}

    def capturing_dispatcher(
        agent_type: str, prompt: str, *, cwd: Path, model: str | None = None, **_: object
    ) -> AgentResult:
        seen["model"] = model
        return AgentResult(agent_type=agent_type, data={}, raw="")

    run_eval(config, tmp_path / "ws", dispatcher=capturing_dispatcher)
    assert seen["model"] == "haiku"


def test_load_config_parses_explicit_model(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nmodel: opus\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    assert load_config(path).model == "opus"


def test_load_config_absent_model_resolves_via_routing(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n  - id: t1\n    prompt: p\n",
    )
    config = load_config(path)
    assert config.model == select_model(config.agent_type, BALANCED)


def test_load_config_rejects_non_string_model(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nmodel: 123\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="model"):
        load_config(path)


def test_load_config_rejects_empty_string_model(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        'name: d\ntarget: target\nmodel: ""\n'
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="model"):
        load_config(path)


# ---------------------------------------------------------------------------
# Story 31.1-001 — harness selection: config field, CLI precedence (in
# test_cli_eval.py), registry resolution, scoreboard provenance, preflight
# aborts, and per-harness dispatch.
# ---------------------------------------------------------------------------


def test_eval_config_harness_defaults_to_claude() -> None:
    config = EvalConfig(name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    # A concrete, pinned harness name — not None — mirroring the model pin
    # (issue #435) so a scoreboard never records "whatever the CLI defaulted to".
    assert config.harness == DEFAULT_HARNESS


def test_eval_config_explicit_harness_is_kept() -> None:
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )
    assert config.harness == "qwen"


def test_load_config_parses_explicit_harness(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nharness: qwen\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    assert load_config(path).harness == "qwen"


def test_load_config_absent_harness_defaults_to_claude(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\ntickets:\n  - id: t1\n    prompt: p\n",
    )
    assert load_config(path).harness == DEFAULT_HARNESS


def test_load_config_rejects_non_string_harness(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "name: d\ntarget: target\nharness: 123\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="harness"):
        load_config(path)


def test_load_config_rejects_empty_string_harness(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        'name: d\ntarget: target\nharness: ""\n'
        "tickets:\n  - id: t1\n    prompt: p\n",
    )
    with pytest.raises(EvalConfigError, match="harness"):
        load_config(path)


# --- Scoreboard records the harness name (AC1) -------------------------------


def test_aggregate_defaults_scoreboard_harness_to_claude() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo")
    assert board.harness == DEFAULT_HARNESS


def test_aggregate_records_explicit_harness() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo", harness="qwen")
    assert board.harness == "qwen"


def test_render_table_includes_harness() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo", harness="qwen")
    assert "harness: qwen" in render_table(board)


def test_scoreboard_to_dict_includes_harness() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo", harness="qwen")
    assert scoreboard_to_dict(board)["harness"] == "qwen"


# --- resolve_eval_harness — preflight resolution + abort paths --------------


def _write_registry(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "harnesses.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_resolve_eval_harness_unknown_name_aborts(tmp_path: Path) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  codex:\n    command: codex exec\n    parser: codex-exec\n",
    )
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="bogus"
    )
    with pytest.raises(EvalConfigError, match="unknown harness 'bogus'"):
        resolve_eval_harness(config, config_path=cfg)


def test_resolve_eval_harness_unknown_name_names_the_registry_file(tmp_path: Path) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  codex:\n    command: codex exec\n    parser: codex-exec\n",
    )
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="bogus"
    )
    with pytest.raises(EvalConfigError, match=re.escape(str(cfg))):
        resolve_eval_harness(config, config_path=cfg)


def test_resolve_eval_harness_no_registry_configured_aborts() -> None:
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )
    with pytest.raises(EvalConfigError, match="registry"):
        resolve_eval_harness(config, config_path=None)


def test_resolve_eval_harness_disabled_aborts(tmp_path: Path) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n"
        "    parser: codex-exec\n    enabled: false\n",
    )
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )
    with pytest.raises(EvalConfigError, match="disabled"):
        resolve_eval_harness(config, config_path=cfg)


def test_resolve_eval_harness_probe_failure_aborts(tmp_path: Path) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n"
        "    parser: codex-exec\n    probe: 'qwen --version'\n",
    )
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )

    def fake_probe_runner(argv: list[str]) -> tuple[int, str]:
        return 127, "command not found: qwen"

    with pytest.raises(EvalConfigError, match="probe failed"):
        resolve_eval_harness(config, config_path=cfg, probe_runner=fake_probe_runner)


def test_resolve_eval_harness_unsupported_model_pin_aborts(tmp_path: Path) -> None:
    # No {model} placeholder in the command -> the harness ignores the eval's
    # pinned model outright, so preflight must abort rather than silently drop it.
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n    parser: codex-exec\n",
    )
    config = EvalConfig(
        name="d",
        target=Path("t"),
        tickets=[Ticket(id="t1", prompt="p")],
        harness="qwen",
        model="opus",
    )
    with pytest.raises(EvalConfigError, match="cannot take a model pin"):
        resolve_eval_harness(config, config_path=cfg)


def test_resolve_eval_harness_model_placeholder_supported_succeeds(tmp_path: Path) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: 'qwen-build-adapter.sh --model {model}'\n"
        "    parser: codex-exec\n    models:\n      default: qwen-max\n",
    )
    config = EvalConfig(
        name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )
    harness = resolve_eval_harness(config, config_path=cfg)
    assert harness.name == "qwen"


def test_resolve_eval_harness_default_claude_skips_probe_and_model_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: a config naming no harness never touches the registry at all."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    # A registry that would abort on either check if it were ever consulted.
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n"
        "    parser: codex-exec\n    enabled: false\n",
    )
    config = EvalConfig(name="d", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    harness = resolve_eval_harness(config, config_path=cfg)
    assert harness.name == DEFAULT_HARNESS
    assert harness.source == "builtin"


def test_shipped_eval_configs_resolve_to_claude_default() -> None:
    """DoD regression: existing configs run unchanged on the claude default."""
    root = Path(__file__).resolve().parents[1] / "eval"
    for name in ("eval-config.yaml", "ci-config.yaml"):
        config = load_config(root / name)
        assert config.harness == DEFAULT_HARNESS
        harness = resolve_eval_harness(config, config_path=None)
        assert harness.source == "builtin"


# --- dispatcher_for_harness — per-ticket dispatch goes through the resolved --
# harness's own command + parser (AC1), never re-resolving the registry.


def test_dispatcher_for_harness_threads_registry_argv_and_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_registry(
        tmp_path,
        "harnesses:\n  qwen:\n    command: 'qwen-build-adapter.sh --model {model}'\n"
        "    parser: codex-exec\n    models:\n      default: qwen-max\n",
    )
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="d", target=target, n=1, tickets=[Ticket(id="t1", prompt="p")], harness="qwen"
    )
    harness = resolve_eval_harness(config, config_path=cfg)

    seen: dict[str, object] = {}

    def fake_dispatch_agent(
        agent_type: str, prompt: str, *, agent_cmd=None, parser=None, **kwargs: object
    ) -> AgentResult:
        seen["agent_cmd"] = agent_cmd
        seen["parser"] = parser
        return AgentResult(agent_type=agent_type, data={}, raw="")

    monkeypatch.setattr("sdlc.harness.dispatch_agent", fake_dispatch_agent)

    run_eval(config, tmp_path / "ws", dispatcher=dispatcher_for_harness(harness))
    assert seen["agent_cmd"] == harness.to_argv(model=config.model, stage=config.agent_type)
    assert seen["parser"] == "codex-exec"


def test_dispatcher_for_harness_builtin_matches_plain_dispatch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the resolved default-slot dispatcher is byte-identical to no dispatcher."""
    from sdlc.dispatch import resolve_agent_cmd

    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="d",
        target=target,
        n=1,
        model="haiku",
        tickets=[Ticket(id="t1", prompt="p")],
    )
    harness = resolve_eval_harness(config, config_path=None)

    seen: dict[str, object] = {}

    def fake_dispatch_agent(
        agent_type: str, prompt: str, *, agent_cmd=None, parser=None, **kwargs: object
    ) -> AgentResult:
        seen["agent_cmd"] = agent_cmd
        seen["parser"] = parser
        return AgentResult(agent_type=agent_type, data={}, raw="")

    monkeypatch.setattr("sdlc.harness.dispatch_agent", fake_dispatch_agent)

    run_eval(config, tmp_path / "ws", dispatcher=dispatcher_for_harness(harness))
    assert seen["agent_cmd"] == resolve_agent_cmd(model="haiku")
    assert seen["parser"] is None


# ---------------------------------------------------------------------------
# Story 31.1-002 — scoreboard provenance: host/timestamp helpers, the
# Provenance block, and its wiring through aggregate/scoreboard_to_dict.
# ---------------------------------------------------------------------------


def test_host_identifier_is_hostname_slash_arch() -> None:
    host = host_identifier()
    assert "/" in host
    name, _, arch = host.partition("/")
    assert name and arch


def test_utc_timestamp_matches_iso8601_z_format() -> None:
    ts = utc_timestamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts)


def test_build_provenance_from_config() -> None:
    config = EvalConfig(
        name="demo",
        target=Path("t"),
        tickets=[Ticket(id="t1", prompt="p"), Ticket(id="t2", prompt="p2")],
        n=3,
        seed=42,
        model="sonnet",
        harness="qwen",
    )
    prov = build_provenance(
        config,
        harness_version="qwen 1.2.3",
        host="myhost/arm64",
        timestamp="2026-09-05T12:00:00Z",
    )
    assert prov.harness == "qwen"
    assert prov.model == "sonnet"
    assert prov.harness_version == "qwen 1.2.3"
    assert prov.host == "myhost/arm64"
    assert prov.config_name == "demo"
    assert prov.seed == 42
    assert prov.ticket_ids == ["t1", "t2"]
    assert prov.n == 3
    assert prov.timestamp == "2026-09-05T12:00:00Z"


def test_build_provenance_defaults_host_and_timestamp_when_omitted() -> None:
    config = EvalConfig(name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    prov = build_provenance(config)
    assert "/" in prov.host
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prov.timestamp)
    assert prov.harness_version is None


def test_build_provenance_no_probe_version_is_none_not_error() -> None:
    # A harness that declares no `probe` command has no version to record — the
    # field is absent, not a failure.
    config = EvalConfig(name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    prov = build_provenance(config, harness_version=None)
    assert prov.harness_version is None


def test_aggregate_attaches_provenance_when_given() -> None:
    config = EvalConfig(name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    prov = build_provenance(config, timestamp="2026-09-05T12:00:00Z", host="h/arm64")
    board = aggregate([_run("t1", 0, added=1)], "demo", provenance=prov)
    assert board.provenance == prov


def test_aggregate_provenance_defaults_to_none() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo")
    assert board.provenance is None


def test_scoreboard_to_dict_includes_provenance_block_when_present() -> None:
    config = EvalConfig(
        name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")], seed=7, n=2
    )
    prov = build_provenance(config, timestamp="2026-09-05T12:00:00Z", host="h/arm64")
    board = aggregate([_run("t1", 0, added=1)], "demo", provenance=prov)
    payload = scoreboard_to_dict(board)
    assert payload["provenance"] == {
        "harness": DEFAULT_HARNESS,
        "model": config.model,
        "harness_version": None,
        "host": "h/arm64",
        "config_name": "demo",
        "seed": 7,
        "ticket_ids": ["t1"],
        "n": 2,
        "timestamp": "2026-09-05T12:00:00Z",
        "cost_metered": True,
        "local_rate_usd_per_million_tokens": None,
    }


def test_scoreboard_to_dict_omits_provenance_when_absent() -> None:
    board = aggregate([_run("t1", 0, added=1)], "demo")
    assert "provenance" not in scoreboard_to_dict(board)


def test_provenance_is_frozen_dataclass() -> None:
    prov = Provenance(
        harness="claude",
        model="sonnet",
        harness_version=None,
        host="h/arm64",
        config_name="demo",
        seed=None,
        ticket_ids=["t1"],
        n=1,
        timestamp="2026-09-05T12:00:00Z",
    )
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        prov.model = "haiku"  # type: ignore[misc]


def test_build_provenance_records_metered_and_local_rate() -> None:
    """AC5: a configured local rate is recorded as provenance so the
    assumption travels with the number."""
    config = EvalConfig(name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    prov = build_provenance(config, metered=False, local_rate_usd_per_million_tokens=2.5)
    assert prov.cost_metered is False
    assert prov.local_rate_usd_per_million_tokens == 2.5


def test_build_provenance_defaults_metered_true() -> None:
    """AC1: a caller that never passes metered/local_rate keeps the hosted default."""
    config = EvalConfig(name="demo", target=Path("t"), tickets=[Ticket(id="t1", prompt="p")])
    prov = build_provenance(config)
    assert prov.cost_metered is True
    assert prov.local_rate_usd_per_million_tokens is None


# ---------------------------------------------------------------------------
# Story 31.2-002 — component breakdown carried through to the scoreboard
# ---------------------------------------------------------------------------


def _breakdown(**kw: object) -> object:
    from sdlc.usage import TokenBreakdown

    return TokenBreakdown(**kw)  # type: ignore[arg-type]


def _usage_run(ticket: str, idx: int, usage: object) -> RunResult:
    return RunResult(
        ticket_id=ticket,
        run_index=idx,
        diff=DiffStats(added=1, removed=0, files=1),
        wall_s=1.0,
        tokens=usage.total,  # type: ignore[attr-defined]
        cost_usd=0.1,
        quality_pass=True,
        usage=usage,  # type: ignore[arg-type]
    )


def test_scoreboard_carries_the_four_components_not_just_a_total() -> None:
    from sdlc.usage import MEASURED

    usage = _breakdown(
        input_tokens=10, output_tokens=20, cache_read_tokens=30,
        cache_creation_tokens=40, source=MEASURED,
    )
    board = aggregate([_usage_run("t1", 0, usage)], "demo")
    score = board.tickets[0]
    assert score.tokens_mean == 100.0
    assert score.input_tokens_mean == 10.0
    assert score.output_tokens_mean == 20.0
    assert score.cache_read_tokens_mean == 30.0
    assert score.cache_creation_tokens_mean == 40.0
    assert score.tokens_source == MEASURED

    d = scoreboard_to_dict(board)["tickets"][0]
    assert d["cache_creation_tokens_mean"] == 40.0
    assert d["cache_read_tokens_mean"] == 30.0
    assert d["tokens_source"] == MEASURED


def test_scoreboard_components_are_none_when_usage_is_unavailable() -> None:
    from sdlc.usage import UNAVAILABLE

    board = aggregate([_run("t1", 0, added=5, tokens=None)], "demo")
    score = board.tickets[0]
    assert score.tokens_mean is None
    assert score.input_tokens_mean is None
    assert score.cache_read_tokens_mean is None
    assert score.tokens_source == UNAVAILABLE


def test_run_ticket_records_the_component_breakdown(tmp_path: Path) -> None:
    from sdlc.usage import MEASURED

    target = _sample_target(tmp_path)
    ticket = Ticket(id="t1", prompt="add a thing")
    config = EvalConfig(name="c", target=target, tickets=[ticket])

    def fake(agent_type, prompt, **kwargs):  # type: ignore[no-untyped-def]
        (kwargs["cwd"] / "new.txt").write_text("x\n", encoding="utf-8")
        return AgentResult(
            agent_type=agent_type, data={}, raw="",
            usage={
                "input_tokens": 8, "output_tokens": 7,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 15145,
            },
            cost_usd=0.5,
        )

    res = run_ticket(ticket, config, 0, tmp_path / "ws", dispatcher=fake)
    assert res.tokens == 15160
    assert res.usage.cache_creation_tokens == 15145
    assert res.usage.source == MEASURED


def test_run_ticket_on_a_harness_without_usage_tracking_reports_unavailable(
    tmp_path: Path,
) -> None:
    # AC3: a false-telemetry harness that prints numbers anyway earns no figure.
    from sdlc.usage import UNAVAILABLE

    target = _sample_target(tmp_path)
    ticket = Ticket(id="t1", prompt="add a thing")
    config = EvalConfig(name="c", target=target, tickets=[ticket])

    def fake(agent_type, prompt, **kwargs):  # type: ignore[no-untyped-def]
        (kwargs["cwd"] / "new.txt").write_text("x\n", encoding="utf-8")
        return AgentResult(
            agent_type=agent_type, data={}, raw="",
            usage={"input_tokens": 999, "output_tokens": 1},
            cost_usd=0.0,
        )

    res = run_ticket(
        ticket, config, 0, tmp_path / "ws",
        dispatcher=fake, capabilities={"usage_tracking": False},
    )
    assert res.tokens is None
    # usage_tracking covers cost too: the arm must not look free.
    assert res.cost_usd is None
    assert res.usage.source == UNAVAILABLE
    board = aggregate([res], "demo")
    assert board.tickets[0].tokens_mean is None
    assert board.tickets[0].tokens_source == UNAVAILABLE


def test_render_table_labels_an_estimated_token_figure(tmp_path: Path) -> None:
    from sdlc.usage import ESTIMATED, MEASURED

    est = _breakdown(input_tokens=100, source=ESTIMATED)
    measured = _breakdown(input_tokens=100, source=MEASURED)
    est_table = render_table(aggregate([_usage_run("t1", 0, est)], "demo"))
    measured_table = render_table(aggregate([_usage_run("t1", 0, measured)], "demo"))
    assert "~100" in est_table
    assert "estimate" in est_table
    assert "~100" not in measured_table


# ---------------------------------------------------------------------------
# Story 31.2-003 — cost provenance end-to-end: aggregation, rendering, and
# threading `metered`/`local_rate_usd_per_million_tokens` through the runner.
# ---------------------------------------------------------------------------


def _cost_run(ticket: str, idx: int, *, cost: float | None, cost_source: str) -> RunResult:
    return RunResult(
        ticket_id=ticket,
        run_index=idx,
        diff=DiffStats(added=1, removed=0, files=1),
        wall_s=1.0,
        tokens=100,
        cost_usd=cost,
        cost_source=cost_source,
    )


def test_aggregate_folds_cost_source_when_runs_agree() -> None:
    from sdlc.usage import MEASURED

    board = aggregate(
        [
            _cost_run("t1", 0, cost=1.0, cost_source=MEASURED),
            _cost_run("t1", 1, cost=2.0, cost_source=MEASURED),
        ],
        "demo",
    )
    assert board.tickets[0].cost_source == MEASURED


def test_aggregate_cost_source_not_metered_when_harness_has_no_meter() -> None:
    from sdlc.evaluate import NOT_METERED

    board = aggregate(
        [_cost_run("t1", 0, cost=None, cost_source=NOT_METERED)], "demo"
    )
    score = board.tickets[0]
    assert score.cost_mean is None
    assert score.cost_source == NOT_METERED


def test_aggregate_cost_source_mixed_when_runs_disagree() -> None:
    from sdlc.usage import MEASURED, MIXED

    from sdlc.evaluate import ESTIMATED

    board = aggregate(
        [
            _cost_run("t1", 0, cost=1.0, cost_source=MEASURED),
            _cost_run("t1", 1, cost=2.0, cost_source=ESTIMATED),
        ],
        "demo",
    )
    assert board.tickets[0].cost_source == MIXED


def test_scoreboard_to_dict_includes_cost_source() -> None:
    from sdlc.evaluate import NOT_METERED

    board = aggregate([_cost_run("t1", 0, cost=None, cost_source=NOT_METERED)], "demo")
    payload = scoreboard_to_dict(board)
    assert payload["tickets"][0]["cost_source"] == NOT_METERED


def test_render_table_shows_not_metered_instead_of_a_dollar_figure() -> None:
    """AC2/AC3: a local harness's zero never prints as a plain, comparable $0."""
    from sdlc.evaluate import NOT_METERED

    board = aggregate([_cost_run("t1", 0, cost=None, cost_source=NOT_METERED)], "demo")
    table = render_table(board)
    assert "not metered" in table
    assert "0.0000" not in table
    assert "never read it as $0 spent" in table


def test_render_table_labels_a_local_rate_cost_figure() -> None:
    from sdlc.evaluate import LOCAL_RATE

    board = aggregate([_cost_run("t1", 0, cost=2.5, cost_source=LOCAL_RATE)], "demo")
    table = render_table(board)
    assert "~2.5000" in table
    assert "configured local rate" in table


def test_render_table_hosted_metered_cost_unaffected() -> None:
    """AC1: a plain metered figure renders exactly as before — no "~", no note."""
    from sdlc.usage import MEASURED

    board = aggregate([_cost_run("t1", 0, cost=1.2345, cost_source=MEASURED)], "demo")
    table = render_table(board)
    assert "1.2345" in table
    assert "not metered" not in table
    assert "~1.2345" not in table


def test_run_ticket_not_metered_harness_ignores_literal_zero_cost(
    tmp_path: Path,
) -> None:
    """Field case (AC2): a local harness's own `cost: 0` telemetry must not
    read as a real zero-dollar spend."""
    from sdlc.evaluate import NOT_METERED

    target = _sample_target(tmp_path)
    ticket = Ticket(id="t1", prompt="add a thing")
    config = EvalConfig(name="c", target=target, tickets=[ticket])

    def fake(agent_type, prompt, **kwargs):  # type: ignore[no-untyped-def]
        (kwargs["cwd"] / "new.txt").write_text("x\n", encoding="utf-8")
        return AgentResult(
            agent_type=agent_type, data={}, raw="",
            usage={"input_tokens": 1000, "output_tokens": 200},
            cost_usd=0.0,
        )

    res = run_ticket(ticket, config, 0, tmp_path / "ws", dispatcher=fake, metered=False)
    assert res.tokens == 1200  # tokens are still real — usage_tracking is unaffected
    assert res.cost_usd is None
    assert res.cost_source == NOT_METERED


def test_run_ticket_not_metered_harness_with_configured_local_rate(
    tmp_path: Path,
) -> None:
    from sdlc.evaluate import LOCAL_RATE

    target = _sample_target(tmp_path)
    ticket = Ticket(id="t1", prompt="add a thing")
    config = EvalConfig(name="c", target=target, tickets=[ticket])

    def fake(agent_type, prompt, **kwargs):  # type: ignore[no-untyped-def]
        (kwargs["cwd"] / "new.txt").write_text("x\n", encoding="utf-8")
        return AgentResult(
            agent_type=agent_type, data={}, raw="",
            usage={"input_tokens": 1_000_000},
            cost_usd=0.0,
        )

    res = run_ticket(
        ticket, config, 0, tmp_path / "ws",
        dispatcher=fake, metered=False, local_rate_usd_per_million_tokens=2.5,
    )
    assert res.cost_usd == pytest.approx(2.5)
    assert res.cost_source == LOCAL_RATE


def test_run_eval_threads_metered_and_local_rate_to_every_ticket(
    tmp_path: Path,
) -> None:
    from sdlc.evaluate import NOT_METERED

    target = _sample_target(tmp_path)
    config = EvalConfig(
        name="demo", target=target, n=1,
        tickets=[Ticket(id="t1", prompt="p"), Ticket(id="t2", prompt="p")],
    )

    def fake(agent_type, prompt, **kwargs):  # type: ignore[no-untyped-def]
        return AgentResult(
            agent_type=agent_type, data={}, raw="",
            usage={"input_tokens": 10}, cost_usd=0.0,
        )

    results = run_eval(config, tmp_path / "ws", dispatcher=fake, metered=False)
    assert all(r.cost_usd is None for r in results)
    assert all(r.cost_source == NOT_METERED for r in results)


def test_run_ticket_contract_miss_respects_metered(tmp_path: Path) -> None:
    """A contract miss must not treat a local harness's literal cost as real
    either — the ContractError path shares the same cost-provenance logic."""
    from sdlc.evaluate import NOT_METERED

    target = _sample_target(tmp_path)
    config = EvalConfig(name="demo", target=target, n=1, tickets=[Ticket(id="t1", prompt="edit")])

    def miss_dispatcher(agent_type: str, prompt: str, *, cwd: Path, **_: object) -> AgentResult:
        raise _contract_miss({"input_tokens": 10}, 0.0)

    run = run_eval(config, tmp_path / "ws", dispatcher=miss_dispatcher, metered=False)[0]
    assert run.status == "contract_miss"
    assert run.cost_usd is None
    assert run.cost_source == NOT_METERED
