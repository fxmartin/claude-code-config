# ABOUTME: Tests for the undenied-host-auth routing refusal (issue #654).
# ABOUTME: Declare deny_baseline per harness; refuse host-auth roles routed without it.

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from sdlc.build import (
    BuildOptions,
    BuildResult,
    Ledger,
    parse_build_args,
    run_build,
)
from sdlc.capability import CAPABILITY_KEYS, resolve_capabilities
from sdlc.degradation import (
    DENY_BASELINE_CAPABILITY,
    HOST_AUTH_ROLES,
    DegradationKind,
    evaluate_degradations,
)
from sdlc.dispatch import DENY_BASELINE, resolve_agent_cmd
from sdlc.doctor import check_deny_baseline
from sdlc.harness import (
    HARNESS_REGISTRY_SCHEMA,
    load_harnesses_config,
    resolve_harness,
)
from sdlc.fix_issue import FixBatchOptions, FixOptions, parse_fix_args, run_fix, run_fix_batch
from sdlc.registry import Registry
from sdlc.role_routing import (
    PIPELINE_ROLES,
    default_registry_path,
    format_undenied_bypass,
    format_undenied_host_auth,
    undenied_host_auth_routes,
)

from test_fix_issue import (  # noqa: E402 — sibling test module, reuses its fakes
    FakeBatchGh,
    FakeGh,
    RecordingDispatcher,
    _batch_issue,
    _issue_json,
)


def _story(story_id: str = "99.1-001"):
    from sdlc.build import Story

    return Story(
        story_id, f"Story {story_id}", story_id.split(".", 1)[0].zfill(2), "x",
        "epic-x.md", "P1", 1, "py", [], False,
    )


def _registry(tmp_path: Path, **harnesses: bool) -> Path:
    """A minimal registry whose entries declare (or omit) ``deny_baseline``.

    ``**harnesses`` maps a harness name to the value of its ``deny_baseline``
    capability; pass ``None`` to omit the key entirely (the conservative-default
    case).
    """
    entries = {}
    for name, deny in harnesses.items():
        caps: dict[str, bool] = {"json_contract": True}
        if deny is not None:
            caps[DENY_BASELINE_CAPABILITY] = deny
        entries[name] = {
            "command": f"{name}-adapter.sh",
            "parser": "codex-exec",
            "enabled": True,
            "capabilities": caps,
        }
    path = tmp_path / "harnesses.yaml"
    path.write_text(yaml.safe_dump({"harnesses": entries}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Declaration: the capability, its conservative default, and the schema
# ---------------------------------------------------------------------------


def test_deny_baseline_is_a_canonical_capability_key() -> None:
    assert DENY_BASELINE_CAPABILITY == "deny_baseline"
    assert DENY_BASELINE_CAPABILITY in CAPABILITY_KEYS


def test_an_undeclared_deny_baseline_resolves_to_false(tmp_path) -> None:
    """The conservative default: a harness only earns the floor by claiming it."""
    registry = load_harnesses_config(_registry(tmp_path, mute=None))
    assert resolve_capabilities(registry["mute"])[DENY_BASELINE_CAPABILITY] is False


def test_a_declared_deny_baseline_is_preserved(tmp_path) -> None:
    registry = load_harnesses_config(_registry(tmp_path, loud=True))
    assert resolve_capabilities(registry["loud"])[DENY_BASELINE_CAPABILITY] is True


def test_the_schema_validates_deny_baseline_as_a_boolean() -> None:
    validator = Draft202012Validator(HARNESS_REGISTRY_SCHEMA)
    entry = {"command": "x.sh", "parser": "codex-exec"}

    ok = {"harnesses": {"a": {**entry, "capabilities": {"deny_baseline": True}}}}
    assert list(validator.iter_errors(ok)) == []

    bad = {"harnesses": {"a": {**entry, "capabilities": {"deny_baseline": "yes"}}}}
    assert list(validator.iter_errors(bad))


def test_the_schema_documents_the_capability() -> None:
    """A declared-but-undocumented flag is how the last gap stayed invisible."""
    caps = HARNESS_REGISTRY_SCHEMA["$defs"]["harness"]["properties"]["capabilities"]
    assert "deny_baseline" in caps["properties"]


def test_the_shipped_registry_declares_the_capability_on_every_harness() -> None:
    """`claude` true (it receives DENY_BASELINE); codex/qwen/opencode false."""
    registry = load_harnesses_config(default_registry_path())
    assert registry["claude"].capabilities[DENY_BASELINE_CAPABILITY] is True
    for name in ("codex", "qwen", "opencode"):
        assert registry[name].capabilities[DENY_BASELINE_CAPABILITY] is False


def test_the_builtin_slot_declares_the_floor_it_actually_carries() -> None:
    """The claim is checkable: resolve_agent_cmd really appends the deny rules."""
    builtin = resolve_harness()
    assert builtin.source == "builtin"
    assert resolve_capabilities(builtin)[DENY_BASELINE_CAPABILITY] is True
    argv = resolve_agent_cmd()
    rules = argv[argv.index("--disallowedTools") + 1]
    assert set(DENY_BASELINE).issubset(set(rules.split(",")))


def test_the_env_override_slot_declares_no_floor(monkeypatch) -> None:
    """`SDLC_AGENT_CMD` owns its own posture — resolve_agent_cmd adds no rules."""
    monkeypatch.setenv("SDLC_AGENT_CMD", "my-agent --go")
    harness = resolve_harness()
    assert harness.source == "env"
    assert resolve_capabilities(harness)[DENY_BASELINE_CAPABILITY] is False
    assert "--disallowedTools" not in resolve_agent_cmd()


# ---------------------------------------------------------------------------
# 2. The degradation matrix treats it as a refusal, not a downgrade
# ---------------------------------------------------------------------------


def test_host_auth_roles_are_merge_and_review() -> None:
    assert HOST_AUTH_ROLES == ("merge", "review")
    assert set(HOST_AUTH_ROLES).issubset(set(PIPELINE_ROLES))


@pytest.mark.parametrize("role", HOST_AUTH_ROLES)
def test_a_host_auth_role_without_the_floor_is_a_refusal(role: str) -> None:
    plan = evaluate_degradations("codex", {}, roles=(role,))
    assert plan.has(DegradationKind.UNDENIED_HOST_AUTH)
    refusals = plan.refusals
    assert [d.kind for d in refusals] == [DegradationKind.UNDENIED_HOST_AUTH]
    assert refusals[0].roles == (role,)
    assert refusals[0].missing == (DENY_BASELINE_CAPABILITY,)
    # It is a refusal, NOT a silent mode downgrade.
    assert plan.mode_degraded is False


def test_a_non_host_auth_role_without_the_floor_is_not_a_refusal() -> None:
    for role in ("build", "coverage", "docs"):
        plan = evaluate_degradations("codex", {}, roles=(role,))
        assert not plan.has(DegradationKind.UNDENIED_HOST_AUTH)
        assert plan.refusals == ()


def test_a_host_auth_role_with_the_floor_is_not_a_refusal() -> None:
    plan = evaluate_degradations(
        "claude", {DENY_BASELINE_CAPABILITY: True}, roles=("merge",)
    )
    assert not plan.has(DegradationKind.UNDENIED_HOST_AUTH)


def test_evaluating_without_roles_is_unchanged() -> None:
    """Every pre-#654 call site passes no roles and must behave exactly as before."""
    plan = evaluate_degradations("codex", {})
    assert not plan.has(DegradationKind.UNDENIED_HOST_AUTH)
    assert plan.kinds() == {
        DegradationKind.USAGE_UNAVAILABLE,
        DegradationKind.RATE_LIMIT_SKIPPED,
    }


def test_the_refusal_is_recorded_structurally() -> None:
    plan = evaluate_degradations("codex", {}, roles=("merge",))
    record = next(r for r in plan.to_records() if r["kind"] == "undenied_host_auth")
    assert record["refusal"] is True
    assert record["roles"] == ["merge"]
    assert record["missing"] == [DENY_BASELINE_CAPABILITY]


# ---------------------------------------------------------------------------
# 3. The route resolver
# ---------------------------------------------------------------------------


def test_a_host_auth_role_on_an_undenied_harness_is_reported(tmp_path) -> None:
    path = _registry(tmp_path, codex=False)
    assert undenied_host_auth_routes({"merge": "codex"}, config_path=path) == [
        ("merge", "codex")
    ]


def test_a_non_host_auth_role_on_an_undenied_harness_is_not_reported(tmp_path) -> None:
    path = _registry(tmp_path, codex=False)
    routes = {"build": "codex", "coverage": "codex", "docs": "codex"}
    assert undenied_host_auth_routes(routes, config_path=path) == []


def test_a_host_auth_role_on_a_denied_harness_is_not_reported(tmp_path) -> None:
    path = _registry(tmp_path, walled=True)
    assert undenied_host_auth_routes({"merge": "walled"}, config_path=path) == []


def test_every_offending_host_auth_role_is_reported(tmp_path) -> None:
    path = _registry(tmp_path, codex=False, opencode=None)
    assert undenied_host_auth_routes(
        {"merge": "codex", "review": "opencode", "build": "codex"}, config_path=path
    ) == [("merge", "codex"), ("review", "opencode")]


def test_the_qa_alias_resolves_to_a_non_host_auth_role(tmp_path) -> None:
    path = _registry(tmp_path, codex=False)
    assert undenied_host_auth_routes({"qa": "codex"}, config_path=path) == []


def test_an_empty_or_default_map_reports_nothing(tmp_path) -> None:
    path = _registry(tmp_path, codex=False)
    assert undenied_host_auth_routes({}, config_path=path) == []
    assert undenied_host_auth_routes(None, config_path=path) == []
    assert undenied_host_auth_routes({"merge": "claude"}, config_path=path) == []


def test_an_unresolvable_harness_is_skipped(tmp_path) -> None:
    """`resolve_role_routing` already fails fast on it with a better message."""
    path = _registry(tmp_path, codex=False)
    assert undenied_host_auth_routes({"merge": "ghost"}, config_path=path) == []


def test_the_resolver_defaults_to_the_shipped_registry() -> None:
    """A caller that forgets `config_path` must not silently pass everything."""
    assert undenied_host_auth_routes({"merge": "opencode"}) == [("merge", "opencode")]


def test_the_refusal_message_is_one_actionable_line() -> None:
    msg = format_undenied_host_auth([("merge", "opencode")], "sdlc build")
    assert "\n" not in msg
    assert "UNDENIED_HOST_AUTH" in msg
    assert "merge=opencode" in msg
    assert "sdlc build --allow-undenied" in msg
    assert "#654" in msg


def test_the_bypass_line_names_the_role_and_harness() -> None:
    line = format_undenied_bypass([("merge", "codex"), ("review", "codex")])
    assert "--allow-undenied" in line
    assert "merge=codex" in line and "review=codex" in line


# ---------------------------------------------------------------------------
# 4. The regression: `sdlc build` refuses before any dispatch
# ---------------------------------------------------------------------------


def _build(tmp_path, dispatch, **kwargs) -> BuildResult:
    return run_build(
        BuildOptions(scope="all", **kwargs),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        root=tmp_path,
        dirty_check=lambda: [],
    )


def test_run_build_refuses_merge_on_an_undenied_harness(tmp_path) -> None:
    """The #654 regression: `--harness merge=opencode` must never dispatch."""
    dispatch = RecordingDispatcher()

    result = _build(tmp_path, dispatch, harness_map={"merge": "opencode"})

    assert result.undenied_host_auth == [("merge", "opencode")]
    # Refused before preflight, the ledger and any dispatch — nothing written.
    assert result.run_id is None
    assert dispatch.agents() == []


def test_run_build_refuses_review_on_an_undenied_harness(tmp_path) -> None:
    result = _build(tmp_path, RecordingDispatcher(), harness_map={"review": "codex"})
    assert result.undenied_host_auth == [("review", "codex")]
    assert result.run_id is None


def test_run_build_proceeds_with_build_on_an_undenied_harness(tmp_path) -> None:
    """AC: routing `build=opencode` proceeds unchanged."""
    result = _build(tmp_path, RecordingDispatcher(), harness_map={"build": "opencode"})
    assert result.undenied_host_auth == []
    assert result.run_id is not None


def test_run_build_is_byte_identical_for_a_claude_route(tmp_path) -> None:
    """AC: `claude` routes are unaffected — the whole map on the default slot."""
    result = _build(
        tmp_path,
        RecordingDispatcher(),
        harness_map={role: "claude" for role in PIPELINE_ROLES},
    )
    assert result.undenied_host_auth == []
    assert result.run_id is not None


def test_run_build_with_no_harness_map_is_unaffected(tmp_path) -> None:
    result = _build(tmp_path, RecordingDispatcher())
    assert result.undenied_host_auth == []
    assert result.run_id is not None


def test_allow_undenied_proceeds_and_warns_in_the_ledger(tmp_path) -> None:
    """AC: the bypass proceeds, with a `warn` ledger event naming role + harness."""
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    result = run_build(
        BuildOptions(
            scope="all", harness_map={"merge": "opencode"}, allow_undenied=True
        ),
        queue=[_story()],
        ledger=ledger,
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        root=tmp_path,
        dirty_check=lambda: [],
    )

    assert result.undenied_host_auth == []
    assert result.run_id is not None
    events = [
        row
        for row in ledger.recent_events(result.run_id, limit=200)
        if "--allow-undenied" in (row["message"] or "")
    ]
    assert len(events) == 1
    assert events[0]["level"] == "warn"
    assert "merge=opencode" in events[0]["message"]


def test_the_bypass_line_is_printed_at_preflight(tmp_path, capsys) -> None:
    run_build(
        BuildOptions(
            scope="all", harness_map={"merge": "opencode"}, allow_undenied=True
        ),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        root=tmp_path,
        dirty_check=lambda: [],
    )
    assert "--allow-undenied" in capsys.readouterr().err


def test_a_clean_run_logs_no_bypass_warning(tmp_path, capsys) -> None:
    run_build(
        BuildOptions(scope="all", harness_map={"build": "opencode"}),
        queue=[_story()],
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        root=tmp_path,
        dirty_check=lambda: [],
    )
    assert "--allow-undenied" not in capsys.readouterr().err


def test_a_dry_run_is_unaffected(tmp_path) -> None:
    """A dry run dispatches nothing, so it exposes no credentials to protect.

    Mirrors the `--allow-dirty` guard's dry-run carve-out (issue #590): the
    guards exist to stop a *dispatch*, and a dry run returns before either.
    """
    result = _build(
        tmp_path, RecordingDispatcher(), dry_run=True, harness_map={"merge": "codex"}
    )
    assert result.dry_run is True
    assert result.undenied_host_auth == []


# ---------------------------------------------------------------------------
# 5. The regression: `sdlc fix` refuses before any dispatch
# ---------------------------------------------------------------------------


def test_run_fix_refuses_merge_on_an_undenied_harness(tmp_path) -> None:
    dispatch = RecordingDispatcher()

    result = run_fix(
        FixOptions(issue=1, harness_map={"merge": "opencode"}),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=tmp_path,
        dirty_check=lambda: [],
    )

    assert result.status == "ABORTED"
    assert result.undenied_host_auth == [("merge", "opencode")]
    assert result.run_id is None
    assert dispatch.agents() == []


def test_run_fix_proceeds_with_build_on_an_undenied_harness(tmp_path) -> None:
    result = run_fix(
        FixOptions(issue=1, harness_map={"build": "opencode"}),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=tmp_path,
        dirty_check=lambda: [],
    )
    assert result.undenied_host_auth == []
    assert result.run_id is not None


def test_run_fix_allow_undenied_proceeds_and_warns(tmp_path) -> None:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    result = run_fix(
        FixOptions(
            issue=1, harness_map={"merge": "opencode"}, allow_undenied=True
        ),
        ledger=ledger,
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
        root=tmp_path,
        dirty_check=lambda: [],
    )

    assert result.undenied_host_auth == []
    assert result.run_id is not None
    events = [
        row
        for row in ledger.recent_events(result.run_id, limit=200)
        if "--allow-undenied" in (row["message"] or "")
    ]
    assert len(events) == 1
    assert events[0]["level"] == "warn"
    assert "merge=opencode" in events[0]["message"]


def test_run_fix_batch_refuses_merge_on_an_undenied_harness(tmp_path) -> None:
    """One shared map, so one refusal covers every issue in the batch."""
    dispatch = RecordingDispatcher()

    result = run_fix_batch(
        FixBatchOptions(target="all", harness_map={"review": "codex"}),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1)]),
        root=tmp_path,
        dirty_check=lambda: [],
        registry=Registry(tmp_path / "registry.json"),
    )

    assert result.status == "ABORTED"
    assert result.undenied_host_auth == [("review", "codex")]
    assert result.run_id is None
    assert dispatch.agents() == []


def test_run_fix_batch_allow_undenied_proceeds(tmp_path) -> None:
    result = run_fix_batch(
        FixBatchOptions(
            target="all", harness_map={"review": "codex"}, allow_undenied=True
        ),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1)]),
        root=tmp_path,
        dirty_check=lambda: [],
        registry=Registry(tmp_path / "registry.json"),
    )
    assert result.undenied_host_auth == []
    assert result.run_id is not None


# ---------------------------------------------------------------------------
# 6. The flag surface
# ---------------------------------------------------------------------------


def test_build_parses_allow_undenied() -> None:
    assert parse_build_args(["all"]).allow_undenied is False
    assert parse_build_args(["all", "--allow-undenied"]).allow_undenied is True


def test_fix_parses_allow_undenied() -> None:
    assert parse_fix_args(["7"]).allow_undenied is False
    assert parse_fix_args(["7", "--allow-undenied"]).allow_undenied is True
    assert parse_fix_args(["all", "--allow-undenied"]).allow_undenied is True


# ---------------------------------------------------------------------------
# 7. The CLI renders the refusal and exits non-zero
# ---------------------------------------------------------------------------


_SAMPLE_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _cli_project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def test_the_build_cli_refuses_and_exits_nonzero(tmp_path, monkeypatch) -> None:
    """End to end through the CLI: one actionable line on stderr, exit 1."""
    from typer.testing import CliRunner

    from sdlc.cli import app

    _cli_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # The recursion guard would otherwise short-circuit before the gate.
    monkeypatch.delenv("SDLC_IN_TEST", raising=False)

    result = CliRunner().invoke(
        app, ["build", "epic-99", "--harness", "merge=opencode", "--skip-preflight"]
    )

    assert result.exit_code == 1, result.output
    assert "UNDENIED_HOST_AUTH" in result.output
    assert "merge=opencode" in result.output
    assert "sdlc build --allow-undenied" in result.output


@pytest.mark.parametrize("target", ["1", "all"])
def test_the_fix_cli_renders_the_refusal(tmp_path, monkeypatch, target: str) -> None:
    """`sdlc fix` (single and batch) renders the same refusal and exits 1.

    The run itself is stubbed: reaching the real guard would first shell out to
    `gh` for the issue, and this asserts the CLI's rendering of the refusal, which
    the `run_fix` / `run_fix_batch` tests above already produce for real.
    """
    import sdlc.fix_issue as fix_mod
    from typer.testing import CliRunner

    from sdlc.cli import app

    monkeypatch.chdir(tmp_path)
    routes = [("merge", "opencode")]
    monkeypatch.setattr(
        fix_mod, "run_fix",
        lambda *a, **k: fix_mod.FixResult(
            issue=1, status="ABORTED", undenied_host_auth=routes
        ),
    )
    monkeypatch.setattr(
        fix_mod, "run_fix_batch",
        lambda *a, **k: fix_mod.FixBatchResult(
            status="ABORTED", undenied_host_auth=routes
        ),
    )

    result = CliRunner().invoke(app, ["fix", target])

    assert result.exit_code == 1, result.output
    assert "UNDENIED_HOST_AUTH" in result.output
    assert "sdlc fix --allow-undenied" in result.output


# ---------------------------------------------------------------------------
# 8. `sdlc doctor` surfaces the inventory
# ---------------------------------------------------------------------------


def test_doctor_lists_harnesses_without_the_floor(tmp_path) -> None:
    """The inventory is reported even when nothing routes to them."""
    finding = check_deny_baseline(
        tmp_path,
        registry_path=_registry(tmp_path, codex=False, qwen=None, walled=True),
    )
    assert finding.status == "CLEAN"
    assert "codex" in finding.detail and "qwen" in finding.detail
    assert "walled" not in finding.detail


def test_doctor_warns_when_the_repo_routes_a_host_auth_role_to_one(tmp_path) -> None:
    (tmp_path / ".sdlc-harness.yaml").write_text(
        "harness:\n  roles:\n    merge: codex\n", encoding="utf-8"
    )
    finding = check_deny_baseline(
        tmp_path, registry_path=_registry(tmp_path, codex=False)
    )
    assert finding.status == "WARN"
    assert "merge=codex" in finding.detail
    assert "--allow-undenied" in finding.remedy


def test_doctor_stays_clean_for_a_non_host_auth_repo_pin(tmp_path) -> None:
    (tmp_path / ".sdlc-harness.yaml").write_text(
        "harness:\n  roles:\n    build: codex\n", encoding="utf-8"
    )
    finding = check_deny_baseline(
        tmp_path, registry_path=_registry(tmp_path, codex=False)
    )
    assert finding.status == "CLEAN"


def test_doctor_warns_when_a_registry_default_routes_everything(tmp_path) -> None:
    """A global `default:` moves merge/review too — the silent-drift case."""
    path = _registry(tmp_path, codex=False)
    path.write_text(
        path.read_text(encoding="utf-8") + "default: codex\n", encoding="utf-8"
    )
    finding = check_deny_baseline(tmp_path, registry_path=path)
    assert finding.status == "WARN"
    assert "merge=codex" in finding.detail and "review=codex" in finding.detail


def test_doctor_is_clean_when_every_harness_declares_the_floor(tmp_path) -> None:
    finding = check_deny_baseline(
        tmp_path, registry_path=_registry(tmp_path, walled=True)
    )
    assert finding.status == "CLEAN"
    assert "every enabled harness declares deny_baseline" in finding.detail


def test_doctor_is_clean_with_no_registry(tmp_path) -> None:
    finding = check_deny_baseline(tmp_path, registry_path=tmp_path / "missing.yaml")
    assert finding.status == "CLEAN"


def test_doctor_fails_on_an_unparseable_registry(tmp_path) -> None:
    path = tmp_path / "harnesses.yaml"
    path.write_text("harnesses: [not, a, mapping]\n", encoding="utf-8")
    assert check_deny_baseline(tmp_path, registry_path=path).status == "FAIL"


def test_doctor_ignores_a_broken_repo_pin(tmp_path) -> None:
    """A malformed `.sdlc-harness.yaml` is `check_harness_pin`'s FAIL, not ours."""
    (tmp_path / ".sdlc-harness.yaml").write_text("nope: 1\n", encoding="utf-8")
    finding = check_deny_baseline(
        tmp_path, registry_path=_registry(tmp_path, codex=False)
    )
    assert finding.status == "CLEAN"


def test_doctor_lists_the_shipped_registrys_undenied_harnesses(tmp_path) -> None:
    """The real inventory: codex/qwen/opencode ship without the floor."""
    finding = check_deny_baseline(tmp_path)
    assert finding.status == "CLEAN"
    for name in ("codex", "qwen", "opencode"):
        assert name in finding.detail
    assert "claude" not in finding.detail


def test_doctor_runs_the_check(tmp_path) -> None:
    """The finding is wired into the aggregated report, not just importable."""
    from sdlc.doctor import run_doctor

    report = run_doctor(
        repo_root=tmp_path,
        claude_dir=tmp_path / "claude",
        db_path=tmp_path / "ledger.db",
        registry=Registry(tmp_path / "registry.json"),
        dep_probe=lambda _tool: True,
    )
    assert any(f.name == "Harness deny baseline" for f in report.findings)
