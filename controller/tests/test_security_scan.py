# ABOUTME: Tests for the SAST security-scan module (Story 9.1-001).
# ABOUTME: Covers semgrep report parsing, severity classification, suppressions, and command build.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc.security_scan import (
    DEFAULT_RULESETS,
    SastConfigError,
    SastReportError,
    build_semgrep_command,
    classify_report,
    load_sast_config,
)


def _finding(
    *,
    severity: str,
    check_id: str = "rules.example",
    path: str = "src/app.py",
    line: int = 1,
    message: str = "example finding",
) -> dict:
    """A minimal semgrep result object matching the `results[]` shape."""
    return {
        "check_id": check_id,
        "path": path,
        "start": {"line": line},
        "end": {"line": line},
        "extra": {"severity": severity, "message": message},
    }


def _report(findings: list[dict]) -> str:
    """Serialize findings into a semgrep `--json` report string."""
    return json.dumps({"results": findings, "errors": []})


# ---------------------------------------------------------------------------
# Severity classification (AC: CLEAN / WARN / BLOCK)
# ---------------------------------------------------------------------------


def test_clean_when_no_findings() -> None:
    result = classify_report(_report([]))
    assert result.status == "CLEAN"
    assert result.findings == []


def test_clean_when_only_info_findings() -> None:
    result = classify_report(_report([_finding(severity="INFO")]))
    assert result.status == "CLEAN"


def test_warn_when_warning_findings_present() -> None:
    result = classify_report(_report([_finding(severity="WARNING")]))
    assert result.status == "WARN"
    assert len(result.findings) == 1


def test_block_when_error_findings_present() -> None:
    result = classify_report(
        _report([_finding(severity="ERROR", check_id="python.lang.security.sqli")])
    )
    assert result.status == "BLOCK"
    assert result.findings[0].check_id == "python.lang.security.sqli"


def test_error_dominates_warning() -> None:
    result = classify_report(
        _report([_finding(severity="WARNING"), _finding(severity="ERROR")])
    )
    assert result.status == "BLOCK"


def test_severity_is_case_insensitive() -> None:
    result = classify_report(_report([_finding(severity="error")]))
    assert result.status == "BLOCK"


def test_counts_by_severity_are_reported() -> None:
    result = classify_report(
        _report(
            [
                _finding(severity="ERROR"),
                _finding(severity="WARNING"),
                _finding(severity="WARNING"),
                _finding(severity="INFO"),
            ]
        )
    )
    assert result.counts == {"ERROR": 1, "WARNING": 2, "INFO": 1}


# ---------------------------------------------------------------------------
# Report parsing errors
# ---------------------------------------------------------------------------


def test_invalid_json_raises_report_error() -> None:
    with pytest.raises(SastReportError, match="not valid JSON"):
        classify_report("{not json")


def test_non_object_report_raises() -> None:
    with pytest.raises(SastReportError, match="JSON object"):
        classify_report("[]")


def test_missing_results_key_treated_as_empty() -> None:
    # semgrep always emits `results`, but be defensive: no results == clean.
    result = classify_report(json.dumps({"errors": []}))
    assert result.status == "CLEAN"


# ---------------------------------------------------------------------------
# Per-repo suppressions (.sast-config.yaml)
# ---------------------------------------------------------------------------


def test_suppression_downgrades_block_to_clean(tmp_path: Path) -> None:
    config = tmp_path / ".sast-config.yaml"
    config.write_text(
        "suppress:\n"
        "  - id: python.lang.security.sqli\n"
        "    reason: false positive, parameterized via ORM\n",
        encoding="utf-8",
    )
    sast_config = load_sast_config(config)
    result = classify_report(
        _report([_finding(severity="ERROR", check_id="python.lang.security.sqli")]),
        config=sast_config,
    )
    assert result.status == "CLEAN"
    assert result.suppressed[0].check_id == "python.lang.security.sqli"


def test_suppression_only_affects_matching_id(tmp_path: Path) -> None:
    config = tmp_path / ".sast-config.yaml"
    config.write_text(
        "suppress:\n  - id: some.other.rule\n    reason: not this one\n",
        encoding="utf-8",
    )
    sast_config = load_sast_config(config)
    result = classify_report(
        _report([_finding(severity="ERROR", check_id="python.lang.security.sqli")]),
        config=sast_config,
    )
    assert result.status == "BLOCK"


def test_suppression_without_reason_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".sast-config.yaml"
    config.write_text("suppress:\n  - id: python.lang.security.sqli\n", encoding="utf-8")
    with pytest.raises(SastConfigError, match="reason"):
        load_sast_config(config)


def test_missing_config_returns_empty_config() -> None:
    sast_config = load_sast_config(Path("/nonexistent/.sast-config.yaml"))
    assert sast_config.suppressions == {}
    assert sast_config.extra_rulesets == []


def test_malformed_config_raises(tmp_path: Path) -> None:
    config = tmp_path / ".sast-config.yaml"
    config.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(SastConfigError, match="mapping"):
        load_sast_config(config)


# ---------------------------------------------------------------------------
# Command construction (AC: default + owasp rulesets, JSON output)
# ---------------------------------------------------------------------------


def test_command_includes_default_rulesets_and_json_output() -> None:
    cmd = build_semgrep_command(report_path="/tmp/report.json")
    assert "semgrep" in cmd
    assert "--config=p/default" in cmd
    assert "--config=p/owasp-top-ten" in cmd
    assert "--json" in cmd
    assert "--output=/tmp/report.json" in cmd
    # Both default rulesets are present.
    for ruleset in DEFAULT_RULESETS:
        assert f"--config={ruleset}" in cmd


def test_command_appends_per_repo_extra_rulesets(tmp_path: Path) -> None:
    config = tmp_path / ".sast-config.yaml"
    config.write_text(
        "rulesets:\n  - p/python\n  - ./rules/custom.yaml\n", encoding="utf-8"
    )
    sast_config = load_sast_config(config)
    cmd = build_semgrep_command(report_path="/tmp/report.json", config=sast_config)
    assert "--config=p/python" in cmd
    assert "--config=./rules/custom.yaml" in cmd


def test_command_scans_target_path() -> None:
    cmd = build_semgrep_command(report_path="/tmp/report.json", target="src/")
    assert cmd[-1] == "src/"
