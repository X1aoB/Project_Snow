"""Fixed, offline behavior gates across all 22 selectable characters.

These checks execute production normalization, channel guards and retrieval
scope rules. They do not grade a language model or certify persona quality.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from backend.snow_app.dialogue_output import (
    _communication_block_violations,
    _normalize_content_blocks,
    _render_content_blocks,
)
from backend.snow_app.mvp_policy import MVP_CHARACTER_BY_ID
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.output_validation import _clean_renderable_text

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "dialogue_quality_v1.json").read_text(encoding="utf-8")
)
CASES = FIXTURE["cases"]


def test_fixed_quality_fixture_covers_every_character_and_all_eight_behaviors():
    assert FIXTURE["schema"] == "dialogue-quality-1"
    assert len({case["id"] for case in CASES}) == len(CASES)
    counts = Counter(case["character_id"] for case in CASES)
    assert set(counts) == set(MVP_CHARACTER_BY_ID)
    assert len(counts) == 22
    assert set(counts.values()) == {8}
    kinds = {
        "legal_text",
        "legal_action",
        "unseen_visual",
        "unseen_audio",
        "physical_claim",
        "analyst_reaction",
        "model_envelope",
        "retrieval_scope",
    }
    for character_id in counts:
        assert {case["kind"] for case in CASES if case["character_id"] == character_id} == kinds


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_fixed_dialogue_behavior(case):
    character = MVP_CHARACTER_BY_ID[case["character_id"]]
    assert character.display_name == case["character_name"]
    if case["kind"] == "model_envelope":
        assert _clean_renderable_text(case["raw"]) == case["expected_clean"]
        return
    if case["kind"] == "retrieval_scope":
        service = MVPService.__new__(MVPService)
        view = {"retrieval_document_ids": [case["allowed_document_id"]]}
        document = {
            "document_id": case["allowed_document_id"],
            "source_type": "character_lore",
            "metadata": {},
        }
        assert service._allowed_document(document, view, None, character.character_id)
        document["document_id"] = case["rejected_document_id"]
        assert not service._allowed_document(document, view, None, character.character_id)
        return
    blocks = _normalize_content_blocks(
        {"content_blocks": case["blocks"]},
        case["channel"],
        case["answer"],
        character.display_name,
    )
    rendered = _render_content_blocks(blocks)
    violations = _communication_block_violations(case["message"], rendered, case["channel"], blocks)
    if "expected_render" in case:
        assert rendered == case["expected_render"]
        assert violations == case["expected_violations"]
    else:
        assert any(value.startswith(case["expected_violation_prefix"]) for value in violations)
