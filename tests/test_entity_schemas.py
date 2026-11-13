import pytest
from pydantic import ValidationError

from mem_ol.mcp_server.schemas import (
    AP2AnalysisResult,
    CommitResult,
    EntityDecision,
)
from mem_ol.config import Settings
from mem_ol.mcp_server.tools import _relation_id
from mem_ol.services.entity_commit import source_order_base


def test_create_requires_type_and_name():
    with pytest.raises(ValidationError):
        EntityDecision(action="create")


def test_update_requires_matched_id():
    with pytest.raises(ValidationError):
        EntityDecision(action="update")


def test_api2_analysis_result_shape():
    result = AP2AnalysisResult.model_validate(
        {
            "context_summary": "玩家准备攻击横枝上的老猴。",
            "rewritten_query": "攻击横枝上的老猴",
            "entities": ["老猴", "老猴子", "横枝"],
        }
    )

    assert result.context_summary
    assert result.rewritten_query == "攻击横枝上的老猴"
    assert result.entities == ["老猴", "老猴子", "横枝"]


def test_valid_commit_plan():
    result = CommitResult.model_validate(
        {
            "turn_summary": "玩家进入水帘洞。",
            "decisions": [
                {
                    "action": "create",
                    "canonical_name": "水帘洞",
                    "entity_type": "location",
                    "surface_form": "那洞府",
                    "attribute_changes": [
                        {"key": "state", "value": "无人", "evidence": "洞里空着"}
                    ],
                }
            ],
        }
    )
    assert result.decisions[0].action == "create"
    assert result.decisions[0].attribute_changes[0].key == "state"


def test_relation_id_is_stable_across_commits():
    first = _relation_id("thread", "source", "target", "knows")
    second = _relation_id("thread", "source", "target", "knows")

    assert first == second
    assert first != _relation_id("thread", "source", "target", "ally")


def test_invalid_relation_type_falls_back_to_related_to():
    result = CommitResult.model_validate(
        {
            "turn_summary": "",
            "decisions": [
                {
                    "action": "create",
                    "canonical_name": "甲",
                    "entity_type": "character",
                    "relations": [
                        {"target_ref": "乙", "relation_type": "拥有"}
                    ],
                }
            ],
        }
    )

    assert result.decisions[0].relations[0].relation_type == "related_to"


def test_dialogue_order_is_after_world_import_baseline():
    settings = Settings()

    assert source_order_base(settings, 1) > 999 * 1_000_000
    assert source_order_base(settings, 2) > source_order_base(settings, 1)
