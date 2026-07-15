# ABOUTME: Tests for the reproducible agentic eval harness (Story 18.1-001).
# ABOUTME: Diff scoring, usage/cost extraction, aggregation, config load, isolation runner.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.dispatch import AgentResult
from sdlc.evaluate import (
    DiffStats,
    EvalConfig,
    EvalConfigError,
    RunResult,
    Ticket,
    aggregate,
    load_config,
    parse_diff_numstat,
    render_table,
    result_cost,
    run_eval,
    run_quality_check,
    scoreboard_to_dict,
    tokens_from_usage,
)


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
