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
ADAPTER = _REPO_ROOT / "scripts" / "codex-build-adapter.sh"


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


def test_guide_documents_non_interactive_agent_cmd() -> None:
    """The non-interactive write/exec invocation is documented with current,
    non-deprecated Codex flags (issue #228: `--full-auto` is deprecated)."""
    text = _guide_text()
    assert 'HARNESS_AGENT_CMD="codex exec --dangerously-bypass-approvals-and-sandbox"' in text
    assert "--sandbox workspace-write" in text
    assert "deprecated" in text.lower()


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
# AC1 (coverage gate): the remaining documented runtime claims, each pinned
# against the real artifact it describes so the doc cannot silently rot.
# ---------------------------------------------------------------------------


def test_guide_links_to_the_codex_build_adapter_script() -> None:
    """The runtime section names the adapter a codex stage dispatches through —
    and that linked script must actually exist on disk (no dangling link)."""
    text = _guide_text()
    assert "scripts/codex-build-adapter.sh" in text
    # The markdown link target resolves to a real, present script.
    assert ADAPTER.is_file(), f"documented adapter missing: {ADAPTER}"


def test_guide_default_codex_exec_matches_the_adapter() -> None:
    """The doc says the adapter defaults to `codex exec` and honours
    HARNESS_AGENT_CMD; both claims must match the adapter's own default."""
    text = _guide_text()
    assert "codex exec" in text
    assert "HARNESS_AGENT_CMD" in text
    # Cross-check the documented default against the script that implements it.
    adapter_src = ADAPTER.read_text(encoding="utf-8")
    assert 'AGENT_CMD="${HARNESS_AGENT_CMD:-codex exec}"' in adapter_src


def test_guide_shows_a_runnable_codex_build_example() -> None:
    """An operator can copy a concrete `sdlc build … --harness …=codex` line."""
    text = _guide_text()
    assert re.search(r"sdlc build .*--harness [^\n]*\bcodex\b", text), (
        "no runnable codex-routed `sdlc build` example in the guide"
    )


def test_guide_says_run_codex_login_before_a_build() -> None:
    """Pre-authentication is concrete: run `codex login` once on the host first."""
    text = _guide_text().lower()
    assert "codex login" in text
    assert "before" in text  # the ordering matters: auth precedes the build


def test_guide_provenance_names_the_cli_validate_then_discard_gap() -> None:
    """Provenance is specific: cli.py validated the harnesses then discarded
    them, and 20.7-001 wired routing through the build loop for real dispatch."""
    text = _guide_text()
    low = text.lower()
    assert "cli.py" in low
    assert "discard" in low  # validated then *discarded* — the precise gap
    assert "build loop" in low  # 20.7-001 wired it through the build loop
    assert "dispatch" in low  # now dispatches the Codex adapter for real


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
