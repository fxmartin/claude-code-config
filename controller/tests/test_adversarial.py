# ABOUTME: Tests for the vendor-agnostic adversarial reviewer slot (Story 8.1-001).
# ABOUTME: Covers schema contract, config loading, parallel dispatch, and consensus rules.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.adversarial import (
    REVIEWER_SCHEMA,
    AdversarialContractError,
    AdversarialError,
    ReviewContext,
    ReviewerConfig,
    ReviewRequest,
    _default_invoke,
    apply_consensus,
    build_command,
    dispatch_adversarial_review,
    load_reviewers_config,
    parse_reviewer_response,
)

# The repo's checked-in default config.
CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "sdlc" / "config" / "adversarial-reviewers.yaml"
)


def _verdict(name: str, verdict: str) -> dict:
    """A minimal valid reviewer response object."""
    return {
        "reviewer_name": name,
        "verdict": verdict,
        "summary": f"{name} says {verdict}",
        "findings": [],
    }


# ---------------------------------------------------------------------------
# Output schema contract (AC: draft 2020-12, the documented output shape)
# ---------------------------------------------------------------------------


def test_schema_declares_draft_2020_12() -> None:
    assert REVIEWER_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_valid_reviewer_response_parses() -> None:
    data = {
        "reviewer_name": "codex",
        "verdict": "approve",
        "summary": "looks good",
        "findings": [
            {
                "severity": "info",
                "category": "style",
                "file": "src/x.py",
                "line": 12,
                "message": "minor nit",
            }
        ],
    }
    assert parse_reviewer_response(json.dumps(data)) == data


def test_finding_line_may_be_null() -> None:
    data = _verdict("codex", "request_changes")
    data["findings"] = [
        {
            "severity": "error",
            "category": "security",
            "file": "auth.py",
            "line": None,
            "message": "file-level issue",
        }
    ]
    assert parse_reviewer_response(json.dumps(data)) == data


def test_invalid_verdict_rejected() -> None:
    data = _verdict("codex", "lgtm")  # not in the enum
    with pytest.raises(AdversarialContractError):
        parse_reviewer_response(json.dumps(data))


def test_missing_required_field_rejected_with_name() -> None:
    data = _verdict("codex", "approve")
    del data["summary"]
    with pytest.raises(AdversarialContractError) as exc_info:
        parse_reviewer_response(json.dumps(data))
    assert "summary" in str(exc_info.value)


def test_malformed_json_rejected() -> None:
    with pytest.raises(AdversarialContractError):
        parse_reviewer_response("{not json}")


# ---------------------------------------------------------------------------
# Config loading (AC: yaml lists reviewers with name/command/timeout/verdicts)
# ---------------------------------------------------------------------------


def test_default_config_loads() -> None:
    consensus, reviewers = load_reviewers_config(CONFIG_PATH)
    assert consensus == "any_block_majority"
    by_name = {r.name: r for r in reviewers}
    assert by_name["codex"].enabled is True
    assert by_name["codex"].timeout_sec == 300
    assert "{pr_number}" in by_name["codex"].command
    assert by_name["gemini"].enabled is False
    assert "approve" in by_name["codex"].allowed_verdicts


def test_config_round_trips_a_custom_file(tmp_path: Path) -> None:
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text(
        "consensus: unanimous_approve\n"
        "reviewers:\n"
        "  stub:\n"
        "    command: 'stub --pr {pr_number}'\n"
        "    timeout_sec: 10\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    consensus, reviewers = load_reviewers_config(cfg)
    assert consensus == "unanimous_approve"
    assert len(reviewers) == 1
    assert reviewers[0].name == "stub"
    # allowed_verdicts defaults to all three when omitted.
    assert set(reviewers[0].allowed_verdicts) == {
        "approve",
        "request_changes",
        "block",
    }


def test_build_command_substitutes_placeholders() -> None:
    cfg = ReviewerConfig(
        name="codex",
        command="codex review-pr --pr-number {pr_number} --url {pr_url} --story {story_id}",
        timeout_sec=300,
        enabled=True,
        allowed_verdicts=["approve", "request_changes", "block"],
    )
    request = ReviewRequest(
        pr_number=42,
        pr_url="https://github.com/fxmartin/repo/pull/42",
        story_id="8.1-001",
        diff="diff --git ...",
        context=ReviewContext(tests_pass=True, coverage_pct=93.5, review_approved=True),
    )
    cmd = build_command(cfg, request)
    assert "--pr-number 42" in cmd
    assert "pull/42" in cmd
    assert "--story 8.1-001" in cmd


# ---------------------------------------------------------------------------
# Consensus rule (AC: default any-block-blocks, otherwise majority)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdicts,expected",
    [
        (["approve", "approve"], "approve"),
        (["approve", "block"], "block"),
        (["block", "request_changes"], "block"),
        (["approve", "request_changes"], "request_changes"),  # tie -> changes
        (["approve", "approve", "request_changes"], "approve"),  # majority
        (["request_changes", "request_changes", "approve"], "request_changes"),
    ],
)
def test_any_block_majority_consensus(verdicts: list[str], expected: str) -> None:
    assert apply_consensus(verdicts, "any_block_majority") == expected


@pytest.mark.parametrize(
    "verdicts,expected",
    [
        (["approve", "approve"], "approve"),
        (["approve", "request_changes"], "block"),
        (["approve", "block"], "block"),
    ],
)
def test_unanimous_approve_consensus(verdicts: list[str], expected: str) -> None:
    assert apply_consensus(verdicts, "unanimous_approve") == expected


def test_consensus_empty_blocks() -> None:
    """No reviewers ran -> fail safe with block."""
    assert apply_consensus([], "any_block_majority") == "block"


def test_unknown_consensus_rule_raises() -> None:
    with pytest.raises(ValueError):
        apply_consensus(["approve"], "made_up_rule")


# ---------------------------------------------------------------------------
# AC: dispatch reads config, invokes each enabled reviewer in parallel,
#     collects verdicts, applies consensus. Test harness simulates two
#     reviewers returning different verdicts.
# ---------------------------------------------------------------------------


def _two_reviewer_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text(
        "consensus: any_block_majority\n"
        "reviewers:\n"
        "  alpha:\n"
        "    command: 'alpha --pr {pr_number}'\n"
        "    timeout_sec: 30\n"
        "    enabled: true\n"
        "  beta:\n"
        "    command: 'beta --pr {pr_number}'\n"
        "    timeout_sec: 30\n"
        "    enabled: true\n"
        "  gamma:\n"
        "    command: 'gamma --pr {pr_number}'\n"
        "    timeout_sec: 30\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    return cfg


def _request() -> ReviewRequest:
    return ReviewRequest(
        pr_number=7,
        pr_url="https://github.com/fxmartin/repo/pull/7",
        story_id="8.1-001",
        diff="some diff",
        context=ReviewContext(tests_pass=True, coverage_pct=90.0, review_approved=True),
    )


def test_dispatch_two_reviewers_disagree_block_wins(tmp_path: Path) -> None:
    """alpha approves, beta blocks -> consensus is block; gamma is disabled."""
    canned = {
        "alpha": json.dumps(_verdict("alpha", "approve")),
        "beta": json.dumps(_verdict("beta", "block")),
    }

    invoked: list[str] = []

    def fake_invoke(command: str, timeout: int) -> str:
        name = command.split()[0]
        invoked.append(name)
        return canned[name]

    result = dispatch_adversarial_review(
        pr_number=7,
        story_id="8.1-001",
        diff="some diff",
        context=ReviewContext(tests_pass=True, coverage_pct=90.0, review_approved=True),
        config_path=_two_reviewer_config(tmp_path),
        pr_url="https://github.com/fxmartin/repo/pull/7",
        invoke=fake_invoke,
    )

    assert result.consensus == "block"
    assert {v.reviewer_name for v in result.verdicts} == {"alpha", "beta"}
    # gamma is disabled and must not be invoked.
    assert "gamma" not in invoked


def test_dispatch_two_reviewers_both_approve(tmp_path: Path) -> None:
    canned = {
        "alpha": json.dumps(_verdict("alpha", "approve")),
        "beta": json.dumps(_verdict("beta", "approve")),
    }

    def fake_invoke(command: str, timeout: int) -> str:
        return canned[command.split()[0]]

    result = dispatch_adversarial_review(
        pr_number=7,
        story_id="8.1-001",
        diff="some diff",
        context=ReviewContext(tests_pass=True, coverage_pct=90.0, review_approved=True),
        config_path=_two_reviewer_config(tmp_path),
        pr_url="https://github.com/fxmartin/repo/pull/7",
        invoke=fake_invoke,
    )
    assert result.consensus == "approve"


def test_dispatch_passes_timeout_to_invoke(tmp_path: Path) -> None:
    seen: dict[str, int] = {}

    def fake_invoke(command: str, timeout: int) -> str:
        seen[command.split()[0]] = timeout
        return json.dumps(_verdict(command.split()[0], "approve"))

    dispatch_adversarial_review(
        pr_number=7,
        story_id="8.1-001",
        diff="d",
        context=ReviewContext(tests_pass=True, coverage_pct=90.0, review_approved=True),
        config_path=_two_reviewer_config(tmp_path),
        pr_url="https://github.com/fxmartin/repo/pull/7",
        invoke=fake_invoke,
    )
    assert seen["alpha"] == 30
    assert seen["beta"] == 30


# ---------------------------------------------------------------------------
# to_dict helpers (lines 66, 88) — used by dispatch serialisation
# ---------------------------------------------------------------------------


def test_review_context_to_dict() -> None:
    ctx = ReviewContext(tests_pass=True, coverage_pct=87.5, review_approved=False)
    d = ctx.to_dict()
    assert d == {"tests_pass": True, "coverage_pct": 87.5, "review_approved": False}


def test_review_request_to_dict_nests_context() -> None:
    ctx = ReviewContext(tests_pass=False, coverage_pct=55.0, review_approved=False)
    req = ReviewRequest(
        pr_number=99,
        pr_url="https://github.com/org/repo/pull/99",
        story_id="8.1-001",
        diff="@@ -1,1 +1,2 @@",
        context=ctx,
    )
    d = req.to_dict()
    assert d["pr_number"] == 99
    assert d["story_id"] == "8.1-001"
    assert d["context"] == ctx.to_dict()


# ---------------------------------------------------------------------------
# Config loading error paths (lines 121, 125-126, 132, 137)
# ---------------------------------------------------------------------------


def test_config_non_dict_yaml_raises(tmp_path: Path) -> None:
    """YAML that is a list (not a mapping) must raise AdversarialError."""
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(AdversarialError, match="must be a mapping"):
        load_reviewers_config(cfg)


def test_config_unknown_consensus_raises(tmp_path: Path) -> None:
    """An unrecognised consensus rule must raise AdversarialError naming the rule."""
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text("consensus: made_up_rule\nreviewers: {}\n", encoding="utf-8")
    with pytest.raises(AdversarialError, match="made_up_rule"):
        load_reviewers_config(cfg)


def test_config_reviewers_not_a_mapping_raises(tmp_path: Path) -> None:
    """When 'reviewers' is a list rather than a mapping, raise AdversarialError."""
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text("reviewers:\n  - not_a_mapping\n", encoding="utf-8")
    with pytest.raises(AdversarialError, match="'reviewers' must be a mapping"):
        load_reviewers_config(cfg)


def test_config_reviewer_missing_command_raises(tmp_path: Path) -> None:
    """A reviewer entry that omits 'command' must raise AdversarialError."""
    cfg = tmp_path / "reviewers.yaml"
    cfg.write_text(
        "reviewers:\n  broken:\n    timeout_sec: 60\n    enabled: true\n",
        encoding="utf-8",
    )
    with pytest.raises(AdversarialError, match="'broken'.*command"):
        load_reviewers_config(cfg)


# ---------------------------------------------------------------------------
# parse_reviewer_response — JSON non-object (line 190)
# ---------------------------------------------------------------------------


def test_json_array_is_rejected() -> None:
    """A JSON array is not a reviewer object and must raise AdversarialContractError."""
    with pytest.raises(AdversarialContractError, match="JSON object"):
        parse_reviewer_response('["approve"]')


def test_json_number_is_rejected() -> None:
    """A bare JSON number is not a reviewer object."""
    with pytest.raises(AdversarialContractError, match="JSON object"):
        parse_reviewer_response("42")


# ---------------------------------------------------------------------------
# _default_invoke error paths (lines 280-295)
# ---------------------------------------------------------------------------


def test_default_invoke_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When subprocess.run times out, AdversarialError must be raised."""
    import subprocess

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["false"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdversarialError, match="timed out"):
        _default_invoke("false", 1)


def test_default_invoke_file_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the command binary is missing, AdversarialError must be raised."""
    import subprocess

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("No such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdversarialError, match="could not launch reviewer"):
        _default_invoke("nonexistent_cmd", 30)


def test_default_invoke_os_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError (permission denied etc.) must surface as AdversarialError."""
    import subprocess

    def fake_run(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdversarialError, match="could not launch reviewer"):
        _default_invoke("restricted_cmd", 30)


def test_default_invoke_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero reviewer exit code must raise AdversarialError with the exit code."""
    import subprocess

    fake_result = subprocess.CompletedProcess(
        args=["fail_cmd"],
        returncode=2,
        stdout="",
        stderr="something went wrong",
    )

    def fake_run(*args, **kwargs):
        return fake_result

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdversarialError, match="exited 2"):
        _default_invoke("fail_cmd", 30)


def test_default_invoke_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: zero-exit command returns its stdout."""
    import subprocess

    fake_result = subprocess.CompletedProcess(
        args=["ok_cmd"],
        returncode=0,
        stdout='{"reviewer_name":"x","verdict":"approve","summary":"ok","findings":[]}',
        stderr="",
    )

    def fake_run(*args, **kwargs):
        return fake_result

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = _default_invoke("ok_cmd", 30)
    assert "approve" in output
