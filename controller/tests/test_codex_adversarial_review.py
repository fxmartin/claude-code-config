# ABOUTME: Tests the Codex reference reviewer wrapper against the 8.1-001 slot contract.
# ABOUTME: Story 8.1-002 — runs scripts/codex-adversarial-review.sh and asserts schema-validity.

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sdlc.adversarial import (
    VERDICTS,
    AdversarialContractError,
    parse_reviewer_response,
)

# The wrapper and its captured-transcript fixtures live in the repo root, two
# levels above controller/tests/.
REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "codex-adversarial-review.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex-adversarial"

# Each fixture transcript and the verdict the wrapper should distil from it.
TRANSCRIPTS = {
    "roast-approve.txt": "approve",
    "roast-request-changes.txt": "request_changes",
    "roast-block.txt": "block",
}


def _run_wrapper(transcript: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the wrapper through its hermetic test seam (no real gh/codex)."""
    return subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "42", *args],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(FIXTURES / transcript),
            "PATH": _path_env(),
        },
    )


def _path_env() -> str:
    # jq is required by the wrapper; preserve the discovering shell's PATH.
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="wrapper requires jq on PATH"
)


@pytest.mark.parametrize("transcript,expected_verdict", TRANSCRIPTS.items())
def test_wrapper_output_validates_against_slot_schema(
    transcript: str, expected_verdict: str
) -> None:
    """The wrapper's stdout must satisfy parse_reviewer_response() (8.1-001)."""
    result = _run_wrapper(transcript)
    assert result.returncode == 0, result.stderr

    # The controller's own contract validator is the source of truth here.
    parsed = parse_reviewer_response(result.stdout)

    assert parsed["reviewer_name"] == "codex"
    assert parsed["verdict"] == expected_verdict
    assert parsed["verdict"] in VERDICTS
    assert isinstance(parsed["findings"], list)


def test_wrapper_findings_carry_required_keys() -> None:
    """request_changes transcript yields findings with the full required set."""
    result = _run_wrapper("roast-request-changes.txt")
    assert result.returncode == 0, result.stderr
    parsed = parse_reviewer_response(result.stdout)

    assert parsed["findings"], "expected at least one finding"
    for finding in parsed["findings"]:
        assert set(finding) >= {"severity", "category", "file", "line", "message"}


def test_wrapper_records_reviewer_skill() -> None:
    """The wrapper records which Codex skill ran (extra field, schema-allowed)."""
    result = _run_wrapper("roast-block.txt", "--reviewer-skill", "project-review")
    assert result.returncode == 0, result.stderr
    parsed = parse_reviewer_response(result.stdout)
    assert parsed["reviewer_skill"] == "project-review"


def test_wrapper_fails_closed_on_unparseable_transcript(tmp_path: Path) -> None:
    """No reviewer JSON -> non-zero exit, and nothing that would validate."""
    garbage = tmp_path / "garbage.txt"
    garbage.write_text("prose with no json verdict block\n", encoding="utf-8")

    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "1"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(garbage),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode != 0
    assert "no reviewer JSON" in result.stderr
    with pytest.raises(AdversarialContractError):
        parse_reviewer_response(result.stdout or "")


def test_wrapper_rejects_non_integer_pr_number() -> None:
    """--pr-number must be a positive integer; letters cause exit 2."""
    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "abc"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 2
    assert "positive integer" in result.stderr


def test_wrapper_rejects_unknown_skill() -> None:
    """--reviewer-skill with an unrecognised name must exit 2."""
    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "1", "--reviewer-skill", "nope"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 2
    assert "reviewer-skill" in result.stderr


def test_wrapper_fails_closed_on_bad_json_in_block(tmp_path: Path) -> None:
    """A json-fenced block with invalid JSON content -> non-zero exit."""
    import os

    bad = tmp_path / "bad-json.txt"
    bad.write_text("```json\nnot real json\n```\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "1"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(bad),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode != 0
    assert "JSON object" in result.stderr


def test_wrapper_fails_closed_on_out_of_range_verdict(tmp_path: Path) -> None:
    """A verdict value outside the allowed set -> non-zero exit."""
    import os

    bad_verdict = tmp_path / "bad-verdict.txt"
    bad_verdict.write_text(
        '```json\n{"reviewer_name":"codex","verdict":"bogus","summary":"x","findings":[]}\n```\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "1"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(bad_verdict),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode != 0
    assert "verdict" in result.stderr


def test_wrapper_honours_codex_adv_review_skill_env() -> None:
    """CODEX_ADV_REVIEW_SKILL env var is used as the default skill."""
    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "42"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(FIXTURES / "roast-approve.txt"),
            "CODEX_ADV_REVIEW_SKILL": "project-review",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode == 0, result.stderr
    parsed = parse_reviewer_response(result.stdout)
    assert parsed["reviewer_skill"] == "project-review"


def test_wrapper_accepts_equals_sign_pr_number_syntax() -> None:
    """--pr-number=N (equals-sign) syntax is accepted."""
    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number=42"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(FIXTURES / "roast-approve.txt"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode == 0, result.stderr
    parsed = parse_reviewer_response(result.stdout)
    assert parsed["verdict"] == "approve"


def test_wrapper_accepts_equals_sign_reviewer_skill_syntax() -> None:
    """--reviewer-skill=S (equals-sign) syntax is accepted."""
    import os

    result = subprocess.run(
        ["bash", str(WRAPPER), "--pr-number", "9", "--reviewer-skill=project-review"],
        capture_output=True,
        text=True,
        env={
            "CODEX_ADV_RAW_OUTPUT": str(FIXTURES / "roast-block.txt"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert result.returncode == 0, result.stderr
    parsed = parse_reviewer_response(result.stdout)
    assert parsed["reviewer_skill"] == "project-review"
