# ABOUTME: Story 34.1-003 — full model ids classify into their tier for usage/cost grouping.
# ABOUTME: Covers each id class, the historical-average fold, and the template model examples.
from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.build import _model_tier
from sdlc.model_routing import TIER_MODEL_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5-5", "sonnet"),
        ("claude-fable-5-1", "fable"),
        ("claude-opus-4-8", "opus"),
        ("claude-opus-5-5", "opus"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("opus", "opus"),
        ("gpt-6-astra", "gpt-6-astra"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_tier(model, expected) -> None:
    assert _model_tier(model) == expected


def test_every_configured_tier_id_maps_to_its_tier() -> None:
    for tier, model_id in TIER_MODEL_IDS.items():
        assert _model_tier(model_id) == tier


@pytest.mark.parametrize("name", ["skill-template.md", "command-template.md"])
def test_templates_use_current_model_example(name: str) -> None:
    text = (REPO_ROOT / "templates" / name).read_text()
    assert "claude-sonnet-5-5" in text
    assert "claude-sonnet-4-6" not in text
