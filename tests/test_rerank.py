from mem_ol.config import get_settings
from mem_ol.services.pre_turn import (
    _dedupe_contexts_by_parent,
    _exclude_recent_context,
)
from mem_ol.services.rerank import RerankService, _context_text, _entity_text


def fake_scorer(query, texts):
    return [0.9 if "水帘洞" in text else 0.1 for text in texts]


def test_rerank_orders_entities_and_contexts():
    settings = get_settings()
    service = RerankService(settings, scorer=fake_scorer)
    entities = [
        {
            "entity_id": "e1",
            "canonical_name": "花果山",
            "entity_type": "location",
            "aliases": [],
            "summary": "",
            "rrf_score": 0.03,
        },
        {
            "entity_id": "e2",
            "canonical_name": "水帘洞",
            "entity_type": "location",
            "aliases": [],
            "summary": "",
            "rrf_score": 0.02,
        },
    ]
    contexts = [
        {
            "chunk_no": "c1",
            "content": "无关内容",
            "parent_content": "无关内容",
            "score": 0.5,
        },
        {
            "chunk_no": "c2",
            "content": "水帘洞洞天",
            "parent_content": "水帘洞洞天",
            "score": 0.3,
        },
    ]

    result = service.rerank(
        query="水帘洞在哪里",
        entities=entities,
        contexts=contexts,
        entity_top_k=10,
        context_top_k=10,
    )

    assert result["entities"][0]["entity_id"] == "e2"
    assert result["contexts"][0]["chunk_no"] == "c2"
    assert result["entities"][0]["kind_weight"] == settings.entity_weight
    assert result["contexts"][0]["kind_weight"] == settings.context_weight
    assert result["entities"][0]["weighted_score"] > 0


def test_context_text_prefers_child_content():
    assert (
        _context_text(
            {"content": "子块内容", "parent_content": "父块内容" * 100}
        )
        == "子块内容"
    )


def test_contexts_from_same_parent_are_deduped():
    candidates = [
        {
            "chunk_no": "c1",
            "parent_no": "p1",
            "score": 0.5,
            "content": "子块一",
        },
        {
            "chunk_no": "c2",
            "parent_no": "p1",
            "score": 0.7,
            "content": "子块二",
        },
        {
            "chunk_no": "c3",
            "parent_no": "p2",
            "score": 0.3,
            "content": "子块三",
        },
    ]
    deduped = _dedupe_contexts_by_parent(candidates)
    assert len(deduped) == 2
    assert deduped[0]["chunk_no"] == "c2"
    assert set(deduped[0]["child_chunk_nos"]) == {"c1", "c2"}
    assert deduped[0]["child_hits"] == 2


def test_exclude_context_inside_recent_window():
    candidates = [
        {"chunk_no": "inside", "turn_start": 2, "turn_end": 2},
        {"chunk_no": "spanning", "turn_start": 1, "turn_end": 2},
        {"chunk_no": "older", "turn_start": 1, "turn_end": 1},
        {"chunk_no": "worldview", "turn_start": None, "turn_end": None},
    ]
    filtered = _exclude_recent_context(candidates, {2, 3, 4, 5, 6})
    assert [item["chunk_no"] for item in filtered] == [
        "spanning",
        "older",
        "worldview",
    ]


def test_entity_text_is_compact():
    candidate = {
        "canonical_name": "水帘洞",
        "entity_type": "location",
        "aliases": ["洞府"],
        "summary": "花果山瀑布后的石洞",
        "attributes": [
            {"key": f"attr{i}", "value": "长属性值" * 20} for i in range(10)
        ],
        "relations": [
            {
                "source_name": "水帘洞",
                "target_name": "花果山",
                "relation_type": "located_in",
            }
            for _ in range(10)
        ],
    }
    assert len(_entity_text(candidate)) <= 320
