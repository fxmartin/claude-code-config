# ABOUTME: Tests for the over-engineering review lens (Story 18.2-001).
# ABOUTME: Covers schema contract, config, finding extraction, policy routing, dispatch.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.overengineering import (
    ACTION_ADVISORY,
    ACTION_CLEAN,
    ACTION_DISABLED,
    ACTION_ROUTE_TO_SIMPLIFY,
    DEFAULT_POLICY,
    LENS_CATEGORIES,
    LENS_SCHEMA,
    POLICIES,
    Finding,
    LensConfig,
    OverEngineeringContractError,
    OverEngineeringError,
    _default_invoke,
    dispatch_overengineering_lens,
    extract_findings,
    load_lens_config,
    parse_lens_response,
    render_lens_prompt,
    route_findings,
)

# The repo's checked-in default config.
CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "overengineering-lens.yaml"
)


def _finding(
    category: str = "speculative_abstraction",
    file: str = "src/x.py",
    line: int | None = 12,
    reason: str = "factory wraps a single call site; inline it",
) -> dict:
    return {"category": category, "file": file, "line": line, "reason": reason}


def _response(findings: list[dict] | None = None, summary: str = "reviewed") -> str:
    return json.dumps({"summary": summary, "findings": findings or []})


# ---------------------------------------------------------------------------
# Output schema contract
# ---------------------------------------------------------------------------


def test_schema_declares_draft_2020_12() -> None:
    assert LENS_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_valid_response_parses() -> None:
    data = parse_lens_response(_response([_finding()]))
    assert data["findings"][0]["category"] == "speculative_abstraction"


def test_empty_findings_is_valid() -> None:
    data = parse_lens_response(_response([]))
    assert data["findings"] == []


def test_non_json_output_raises_actionable_error() -> None:
    with pytest.raises(OverEngineeringContractError) as exc:
        parse_lens_response("not json{")
    assert "not valid JSON" in str(exc.value)


def test_non_object_output_raises() -> None:
    with pytest.raises(OverEngineeringContractError):
        parse_lens_response("[1, 2, 3]")


def test_missing_required_field_names_the_field() -> None:
    bad = json.dumps({"findings": []})  # missing 'summary'
    with pytest.raises(OverEngineeringContractError) as exc:
        parse_lens_response(bad)
    assert "summary" in str(exc.value)


def test_unknown_category_rejected() -> None:
    bad = _response([_finding(category="bikeshedding")])
    with pytest.raises(OverEngineeringContractError):
        parse_lens_response(bad)


def test_file_level_finding_allows_null_line() -> None:
    data = parse_lens_response(_response([_finding(line=None)]))
    assert data["findings"][0]["line"] is None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_default_config_loads() -> None:
    config = load_lens_config(CONFIG_PATH)
    assert isinstance(config, LensConfig)
    # Disabled by default — off = unchanged behaviour.
    assert config.enabled is False
    assert config.policy == DEFAULT_POLICY


def test_config_policy_must_be_known(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text("enabled: true\npolicy: delete_everything\n", encoding="utf-8")
    with pytest.raises(OverEngineeringError) as exc:
        load_lens_config(p)
    assert "policy" in str(exc.value)


def test_config_must_be_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(OverEngineeringError):
        load_lens_config(p)


def test_config_defaults_when_keys_omitted(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text("command: x\n", encoding="utf-8")
    config = load_lens_config(p)
    assert config.enabled is False
    assert config.policy == DEFAULT_POLICY


def test_policies_and_categories_exposed() -> None:
    assert DEFAULT_POLICY in POLICIES
    assert "speculative_abstraction" in LENS_CATEGORIES
    assert "premature_generality" in LENS_CATEGORIES


# ---------------------------------------------------------------------------
# Finding extraction (delete-list)
# ---------------------------------------------------------------------------


def test_extract_findings_returns_structured_objects() -> None:
    data = parse_lens_response(_response([_finding(), _finding(line=None)]))
    findings = extract_findings(data)
    assert len(findings) == 2
    assert all(isinstance(f, Finding) for f in findings)
    assert findings[0].file == "src/x.py"
    assert findings[0].line == 12


def test_finding_format_line_includes_file_line_and_reason() -> None:
    f = Finding(
        category="unused_code",
        file="src/y.py",
        line=7,
        reason="param never read",
    )
    line = f.format_line()
    assert "src/y.py:7" in line
    assert "unused_code" in line
    assert "param never read" in line


def test_finding_format_line_omits_line_for_file_level() -> None:
    f = Finding(category="other", file="src/z.py", line=None, reason="dead module")
    line = f.format_line()
    assert "src/z.py" in line
    assert ":" not in line.split("src/z.py")[1].split("[")[0]


# ---------------------------------------------------------------------------
# Policy routing
# ---------------------------------------------------------------------------


def _enabled(policy: str = "advisory") -> LensConfig:
    return LensConfig(enabled=True, policy=policy)


def test_disabled_config_yields_disabled_action_and_drops_findings() -> None:
    findings = [Finding("unused_code", "a.py", 1, "x")]
    outcome = route_findings(findings, LensConfig(enabled=False, policy="advisory"))
    assert outcome.action == ACTION_DISABLED
    assert outcome.findings == []
    assert outcome.has_findings is False


def test_no_findings_is_clean_and_quiet() -> None:
    outcome = route_findings([], _enabled("advisory"))
    assert outcome.action == ACTION_CLEAN
    assert outcome.has_findings is False


def test_findings_with_advisory_policy_route_to_advisory() -> None:
    findings = [Finding("speculative_abstraction", "a.py", 3, "inline it")]
    outcome = route_findings(findings, _enabled("advisory"))
    assert outcome.action == ACTION_ADVISORY
    assert outcome.has_findings is True


def test_findings_with_route_to_simplify_policy() -> None:
    findings = [Finding("reinvented_wheel", "a.py", 9, "use itertools.chain")]
    outcome = route_findings(findings, _enabled("route_to_simplify"))
    assert outcome.action == ACTION_ROUTE_TO_SIMPLIFY


def test_advisory_comment_is_a_delete_list() -> None:
    findings = [
        Finding("unused_code", "a.py", 1, "drop unused param"),
        Finding("premature_generality", "b.py", None, "single caller, no need for hook"),
    ]
    outcome = route_findings(findings, _enabled("advisory"))
    comment = outcome.advisory_comment()
    assert "a.py:1" in comment
    assert "b.py" in comment
    assert "drop unused param" in comment


def test_simplify_directive_lists_each_cut() -> None:
    findings = [Finding("unused_code", "a.py", 1, "drop unused param")]
    outcome = route_findings(findings, _enabled("route_to_simplify"))
    directive = outcome.simplify_directive()
    assert "a.py:1" in directive
    assert "drop unused param" in directive


def test_clean_outcome_has_no_advisory_comment() -> None:
    outcome = route_findings([], _enabled("advisory"))
    assert outcome.advisory_comment() == ""


def test_clean_outcome_has_no_simplify_directive() -> None:
    # Nothing to cut -> the bugfix loop gets an empty directive, not noise.
    outcome = route_findings([], _enabled("route_to_simplify"))
    assert outcome.simplify_directive() == ""


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_lens_prompt_mentions_delete_list_and_story() -> None:
    prompt = render_lens_prompt("18.2-001", "diff --git a/x b/x")
    assert "18.2-001" in prompt
    assert "delete-list" in prompt.lower()
    # The result wrapper schema name must be referenced.
    assert "overengineering-lens-response.schema.json" in prompt


def test_render_lens_prompt_asks_to_stay_quiet_when_minimal() -> None:
    prompt = render_lens_prompt("18.2-001", "diff")
    assert "minimal" in prompt.lower() or "lean" in prompt.lower()


# ---------------------------------------------------------------------------
# Dispatch (with invoke seam — no live model)
# ---------------------------------------------------------------------------


def test_dispatch_disabled_short_circuits_without_invoking(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text("enabled: false\npolicy: advisory\ncommand: x\n", encoding="utf-8")

    calls: list[str] = []

    def _invoke(command: str, timeout: int) -> str:
        calls.append(command)
        return _response([_finding()])

    outcome = dispatch_overengineering_lens(
        pr_number=42,
        story_id="18.2-001",
        diff="diff",
        config_path=p,
        invoke=_invoke,
    )
    assert outcome.action == ACTION_DISABLED
    assert calls == []  # lens never ran — no quota spend


def test_dispatch_enabled_runs_lens_and_routes(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text(
        "enabled: true\npolicy: advisory\ncommand: lens --pr {pr_number}\n",
        encoding="utf-8",
    )

    seen: list[str] = []

    def _invoke(command: str, timeout: int) -> str:
        seen.append(command)
        return _response([_finding()])

    outcome = dispatch_overengineering_lens(
        pr_number=7,
        story_id="18.2-001",
        diff="diff",
        config_path=p,
        invoke=_invoke,
    )
    assert outcome.action == ACTION_ADVISORY
    assert outcome.has_findings
    assert seen == ["lens --pr 7"]


def test_dispatch_clean_diff_stays_quiet(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text(
        "enabled: true\npolicy: advisory\ncommand: lens\n", encoding="utf-8"
    )

    def _invoke(command: str, timeout: int) -> str:
        return _response([])  # already-minimal diff

    outcome = dispatch_overengineering_lens(
        pr_number=7,
        story_id="18.2-001",
        diff="diff",
        config_path=p,
        invoke=_invoke,
    )
    assert outcome.action == ACTION_CLEAN
    assert not outcome.has_findings


def test_dispatch_propagates_contract_error(tmp_path: Path) -> None:
    p = tmp_path / "lens.yaml"
    p.write_text(
        "enabled: true\npolicy: advisory\ncommand: lens\n", encoding="utf-8"
    )

    def _invoke(command: str, timeout: int) -> str:
        return "garbage{"

    with pytest.raises(OverEngineeringContractError):
        dispatch_overengineering_lens(
            pr_number=7,
            story_id="18.2-001",
            diff="diff",
            config_path=p,
            invoke=_invoke,
        )


# ---------------------------------------------------------------------------
# Default invoker (real subprocess — the production dispatch seam)
# ---------------------------------------------------------------------------


def test_default_invoke_returns_stdout() -> None:
    # The happy path: a zero-exit command's stdout is handed back verbatim.
    assert _default_invoke("printf hello", 5) == "hello"


def test_default_invoke_raises_on_nonzero_exit() -> None:
    with pytest.raises(OverEngineeringError) as exc:
        _default_invoke("sh -c 'echo boom >&2; exit 2'", 5)
    assert "exited 2" in str(exc.value)
    assert "boom" in str(exc.value)


def test_default_invoke_raises_when_command_missing() -> None:
    with pytest.raises(OverEngineeringError) as exc:
        _default_invoke("definitely-not-a-real-binary-xyz", 5)
    assert "could not launch lens" in str(exc.value)


def test_default_invoke_raises_on_timeout() -> None:
    with pytest.raises(OverEngineeringError) as exc:
        _default_invoke("sleep 3", 1)
    assert "timed out" in str(exc.value)


def test_dispatch_uses_default_invoker_when_seam_omitted(tmp_path: Path) -> None:
    # No invoke seam -> dispatch falls back to the real subprocess runner.
    lens = tmp_path / "lens.sh"
    lens.write_text(
        '#!/bin/sh\nprintf \'{"summary": "ran", "findings": []}\'\n',
        encoding="utf-8",
    )
    lens.chmod(0o755)
    p = tmp_path / "lens.yaml"
    p.write_text(
        f"enabled: true\npolicy: advisory\ncommand: {lens}\n", encoding="utf-8"
    )

    outcome = dispatch_overengineering_lens(
        pr_number=7,
        story_id="18.2-001",
        diff="diff",
        config_path=p,
    )
    assert outcome.action == ACTION_CLEAN
    assert outcome.summary == "ran"
