# ABOUTME: Tests for the Codex-worker runtime docs + honest Epic-20 status (Story 20.7-003).
# ABOUTME: Pins the auth/sandbox guidance in harness-adapters.md and the corrected status lines.

from __future__ import annotations

import re
from pathlib import Path

# Layout: this file is controller/tests/test_codex_worker_runtime_docs.py.
_CONTROLLER = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CONTROLLER.parent
GUIDE = _REPO_ROOT / "docs" / "harness-adapters.md"
EPIC = _REPO_ROOT / "docs" / "stories" / "epic-20-cross-harness-portability.md"
STORIES = _REPO_ROOT / "docs" / "stories" / "STORIES.md"


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: docs/harness-adapters.md documents the Codex-worker runtime
# ---------------------------------------------------------------------------


def test_guide_has_a_codex_worker_runtime_section() -> None:
    """A dedicated section tells operators how to run a codex-worker build."""
    headings = re.findall(r"^#{2,3} (.+)$", _guide_text(), flags=re.MULTILINE)
    assert any(
        "codex" in h.lower() and ("worker" in h.lower() or "run" in h.lower())
        for h in headings
    ), f"no codex-worker runtime heading found in {[h for h in headings]}"


def test_guide_says_pre_authenticate_codex() -> None:
    """Pre-authenticate codex — the headless worker cannot do interactive login."""
    text = _guide_text().lower()
    assert "pre-authenticate" in text or "pre-authenticate the codex" in text
    # The reason the auth must happen first: no TTY / non-interactive worker.
    assert "headless" in text or "interactive" in text


def test_guide_documents_full_auto_agent_cmd() -> None:
    """The exact non-interactive write/exec invocation is documented verbatim."""
    text = _guide_text()
    assert 'HARNESS_AGENT_CMD="codex exec --full-auto"' in text


def test_guide_warns_against_combining_with_controller_sandbox() -> None:
    """Do NOT combine a Codex worker with the controller --sandbox flag."""
    text = _guide_text().lower()
    assert "--sandbox" in _guide_text()
    # The sandbox is Claude-only and a no-egress image.
    assert "claude-only" in text or "claude only" in text
    assert "no-egress" in text or "no egress" in text


def test_guide_explains_codex_worker_runs_on_host_path() -> None:
    """gh ops need network/auth that codex's workspace-write sandbox blocks."""
    text = _guide_text().lower()
    assert "host path" in text or "on the host" in text
    assert "workspace-write" in text
    # The blocked operations are the worker's gh network/auth calls.
    assert "gh" in text
    assert "network" in text


def test_guide_captures_routing_gap_provenance() -> None:
    """One-line provenance: routing was a ledger label only until Story 20.7-001."""
    text = _guide_text()
    assert "20.7-001" in text
    low = text.lower()
    assert "label" in low  # the gap: it labelled the ledger but ran claude


# ---------------------------------------------------------------------------
# AC2: STORIES.md + the epic file reflect the corrected, honest status
# ---------------------------------------------------------------------------


def test_epic_status_reflects_routing_now_functional() -> None:
    """The epic status no longer claims dispatch still runs claude for every stage."""
    text = EPIC.read_text(encoding="utf-8")
    assert "20.7-001" in text
    low = text.lower()
    # The honest framing: label-only until 20.7-001, now functional.
    assert "label" in low
    assert "functional" in low or "now dispatches" in low or "now routes" in low
    # The stale present-tense claim must be gone.
    assert "_dispatch_stage` still runs `claude` for every stage" not in text


def test_stories_index_reflects_routing_now_functional() -> None:
    """STORIES.md Epic-20 row mirrors the corrected status."""
    text = STORIES.read_text(encoding="utf-8")
    assert "20.7-001" in text
    low = text.lower()
    assert "label" in low
    assert "functional" in low or "now dispatches" in low or "now routes" in low
