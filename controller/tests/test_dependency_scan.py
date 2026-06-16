# ABOUTME: Tests for the dependency security-scan module (Story 9.1-002).
# ABOUTME: Covers osv-scanner report parsing, severity classification, suppressions, expiry, and command build.

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sdlc.dependency_scan import (
    DepScanConfigError,
    DepScanReportError,
    build_osv_command,
    classify_osv_report,
    load_dep_scan_suppressions,
)


def _vuln(
    *,
    osv_id: str = "OSV-2024-0001",
    severity: str = "HIGH",
    summary: str = "example vulnerability",
) -> dict:
    """A minimal osv-scanner vulnerability object."""
    return {
        "id": osv_id,
        "summary": summary,
        "database_specific": {"severity": severity},
    }


def _package(
    *,
    name: str = "requests",
    version: str = "2.0.0",
    ecosystem: str = "PyPI",
    vulns: list[dict] | None = None,
) -> dict:
    """A minimal osv-scanner package entry with its vulnerabilities."""
    return {
        "package": {"name": name, "version": version, "ecosystem": ecosystem},
        "vulnerabilities": vulns if vulns is not None else [],
    }


def _report(packages: list[dict], *, source: str = "uv.lock") -> str:
    """Serialize packages into an osv-scanner `--format=json` report string."""
    return json.dumps(
        {"results": [{"source": {"path": source, "type": "lockfile"}, "packages": packages}]}
    )


# ---------------------------------------------------------------------------
# Severity classification (AC: CLEAN / WARN / BLOCK)
# ---------------------------------------------------------------------------


def test_clean_when_no_results() -> None:
    result = classify_osv_report(json.dumps({"results": []}))
    assert result.status == "CLEAN"
    assert result.findings == []


def test_clean_when_package_has_no_vulnerabilities() -> None:
    result = classify_osv_report(_report([_package(vulns=[])]))
    assert result.status == "CLEAN"


def test_warn_when_low_severity_finding() -> None:
    result = classify_osv_report(_report([_package(vulns=[_vuln(severity="LOW")])]))
    assert result.status == "WARN"
    assert len(result.findings) == 1


def test_warn_when_moderate_severity_finding() -> None:
    result = classify_osv_report(_report([_package(vulns=[_vuln(severity="MODERATE")])]))
    assert result.status == "WARN"


def test_block_when_high_severity_finding() -> None:
    result = classify_osv_report(
        _report([_package(vulns=[_vuln(osv_id="OSV-HIGH", severity="HIGH")])])
    )
    assert result.status == "BLOCK"
    assert result.findings[0].osv_id == "OSV-HIGH"


def test_block_when_critical_severity_finding() -> None:
    result = classify_osv_report(_report([_package(vulns=[_vuln(severity="CRITICAL")])]))
    assert result.status == "BLOCK"


def test_high_dominates_low() -> None:
    result = classify_osv_report(
        _report(
            [
                _package(name="a", vulns=[_vuln(osv_id="OSV-LOW", severity="LOW")]),
                _package(name="b", vulns=[_vuln(osv_id="OSV-HIGH", severity="HIGH")]),
            ]
        )
    )
    assert result.status == "BLOCK"


def test_severity_is_case_insensitive() -> None:
    result = classify_osv_report(_report([_package(vulns=[_vuln(severity="critical")])]))
    assert result.status == "BLOCK"


def test_finding_carries_package_coordinates() -> None:
    result = classify_osv_report(
        _report([_package(name="urllib3", version="1.26.0", vulns=[_vuln(severity="HIGH")])])
    )
    finding = result.findings[0]
    assert finding.package == "urllib3"
    assert finding.version == "1.26.0"
    assert finding.ecosystem == "PyPI"


def test_cvss_score_is_used_when_label_absent() -> None:
    """osv-scanner sometimes omits database_specific.severity; fall back to the
    CVSS score in the vulnerability's `severity[]` array (>=7.0 == high)."""
    vuln = {
        "id": "OSV-CVSS",
        "summary": "cvss-only",
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    }
    result = classify_osv_report(_report([_package(vulns=[vuln])]))
    assert result.status == "BLOCK"


def test_unknown_severity_label_is_treated_as_warn() -> None:
    result = classify_osv_report(_report([_package(vulns=[_vuln(severity="")])]))
    assert result.status == "WARN"


# ---------------------------------------------------------------------------
# Report parsing errors
# ---------------------------------------------------------------------------


def test_invalid_json_raises_report_error() -> None:
    with pytest.raises(DepScanReportError, match="not valid JSON"):
        classify_osv_report("{not json")


def test_non_object_report_raises() -> None:
    with pytest.raises(DepScanReportError, match="JSON object"):
        classify_osv_report("[]")


def test_missing_results_key_treated_as_empty() -> None:
    result = classify_osv_report(json.dumps({}))
    assert result.status == "CLEAN"


# ---------------------------------------------------------------------------
# Per-repo suppressions (.dep-scan-suppressions.yaml)
# ---------------------------------------------------------------------------


def test_suppression_downgrades_block_to_clean(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n"
        "  - id: OSV-2024-0001\n"
        "    reason: not reachable from our call paths\n"
        "    expires: 2999-01-01\n",
        encoding="utf-8",
    )
    suppressions = load_dep_scan_suppressions(config)
    result = classify_osv_report(
        _report([_package(vulns=[_vuln(osv_id="OSV-2024-0001", severity="HIGH")])]),
        suppressions=suppressions,
    )
    assert result.status == "CLEAN"
    assert result.suppressed[0].osv_id == "OSV-2024-0001"


def test_suppression_only_affects_matching_id(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-OTHER\n    reason: nope\n    expires: 2999-01-01\n",
        encoding="utf-8",
    )
    suppressions = load_dep_scan_suppressions(config)
    result = classify_osv_report(
        _report([_package(vulns=[_vuln(osv_id="OSV-2024-0001", severity="HIGH")])]),
        suppressions=suppressions,
    )
    assert result.status == "BLOCK"


def test_suppression_without_reason_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-2024-0001\n    expires: 2999-01-01\n", encoding="utf-8"
    )
    with pytest.raises(DepScanConfigError, match="reason"):
        load_dep_scan_suppressions(config)


def test_suppression_without_expires_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-2024-0001\n    reason: deferred\n", encoding="utf-8"
    )
    with pytest.raises(DepScanConfigError, match="expires"):
        load_dep_scan_suppressions(config)


def test_suppression_with_bad_expires_date_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-2024-0001\n    reason: deferred\n    expires: not-a-date\n",
        encoding="utf-8",
    )
    with pytest.raises(DepScanConfigError, match="expires"):
        load_dep_scan_suppressions(config)


def test_expired_suppression_is_rejected_at_load(tmp_path: Path) -> None:
    """AC: CI fails when a suppression is past its expiry date."""
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-2024-0001\n    reason: deferred\n    expires: 2000-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(DepScanConfigError, match="expired"):
        load_dep_scan_suppressions(config)


def test_expiry_uses_injected_today(tmp_path: Path) -> None:
    """A suppression that expires today is still valid; tomorrow it is not."""
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - id: OSV-2024-0001\n    reason: deferred\n    expires: 2026-06-16\n",
        encoding="utf-8",
    )
    # On the expiry date: still valid.
    valid = load_dep_scan_suppressions(config, today=date(2026, 6, 16))
    assert "OSV-2024-0001" in valid
    # One day later: expired.
    with pytest.raises(DepScanConfigError, match="expired"):
        load_dep_scan_suppressions(config, today=date(2026, 6, 17))


def test_missing_config_returns_empty_suppressions() -> None:
    suppressions = load_dep_scan_suppressions(Path("/nonexistent/.dep-scan-suppressions.yaml"))
    assert suppressions == {}


def test_malformed_config_raises(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(DepScanConfigError, match="mapping"):
        load_dep_scan_suppressions(config)


def test_suppress_entry_not_a_dict_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text("suppress:\n  - OSV-2024-0001\n", encoding="utf-8")
    with pytest.raises(DepScanConfigError, match="'id'"):
        load_dep_scan_suppressions(config)


def test_suppress_entry_dict_missing_id_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".dep-scan-suppressions.yaml"
    config.write_text(
        "suppress:\n  - reason: no id\n    expires: 2999-01-01\n", encoding="utf-8"
    )
    with pytest.raises(DepScanConfigError, match="'id'"):
        load_dep_scan_suppressions(config)


# ---------------------------------------------------------------------------
# Command construction (AC: --lockfile=auto --format=json --output, target ".")
# ---------------------------------------------------------------------------


def test_command_uses_lockfile_auto_and_json_output() -> None:
    cmd = build_osv_command(report_path="/tmp/osv.json")
    assert "osv-scanner" in cmd
    assert "--lockfile=auto" in cmd
    assert "--format=json" in cmd
    assert "--output=/tmp/osv.json" in cmd
    assert cmd[-1] == "."


def test_command_scans_target_path() -> None:
    cmd = build_osv_command(report_path="/tmp/osv.json", target="src/")
    assert cmd[-1] == "src/"


# ---------------------------------------------------------------------------
# DepScanFinding helper methods
# ---------------------------------------------------------------------------


def test_finding_coordinate_formats_package_and_version() -> None:
    result = classify_osv_report(
        _report([_package(name="flask", version="2.1.0", vulns=[_vuln(severity="HIGH")])])
    )
    assert result.findings[0].coordinate() == "flask@2.1.0"
