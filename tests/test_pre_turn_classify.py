from mem_ol.config import Settings
from mem_ol.domain import (
    AP2ContextResult,
    AP2EntitiesResult,
)
from mem_ol.services.pre_turn import PreTurnService
from mem_ol.services.query_enhancer import QueryEnhancer


class FakeStore:
    def get_recent_turns(self, thread_id, turns):
        return []


class FakeLLM:
    def __init__(self, context: AP2ContextResult):
        self.context = context

    def chat_json_model(self, model_cls, messages, **kwargs):
        if model_cls is AP2ContextResult:
            return self.context
        return AP2EntitiesResult(entities=[])


def _service(context: AP2ContextResult) -> PreTurnService:
    service = PreTurnService.__new__(PreTurnService)
    service.store = FakeStore()
    service.llm = FakeLLM(context)
    service.settings = Settings()
    service.query_enhancer = QueryEnhancer()
    return service


def test_classify_pure_meta_instruction():
    service = _service(
        AP2ContextResult(
            channel="meta",
            intent="default",
            meta_kind="instruction",
            directives={"length": "short", "options": "none"},
        )
    )
    result = service.classify("thread", "写短点，别给选项")
    assert result.channel == "meta"
    assert result.meta_kind == "instruction"
    assert result.directives.length == "short"
    assert result.directives.options == "none"


def test_classify_mixed_uses_narrative_query():
    service = _service(
        AP2ContextResult(
            channel="mixed",
            intent="action",
            rewritten_query="推门进入内院",
            narrative_query="我推门进去",
            directives={"length": "short"},
        )
    )
    result = service.classify("thread", "写短点，我推门进去")
    assert result.channel == "mixed"
    assert result.intent == "action"
    assert result.narrative_query == "我推门进去"
    assert result.rewritten_query == "推门进入内院"


def test_run_meta_returns_without_recall():
    service = _service(
        AP2ContextResult(
            channel="meta",
            meta_kind="instruction",
            directives={"length": "short"},
        )
    )
    packet = service.run(
        "thread",
        "写短点",
        analysis=service.classify("thread", "写短点"),
    )
    assert packet["channel"] == "meta"
    assert packet["meta_reply"] == ""
    assert packet["intent"] == "meta"


def test_relation_candidates_enter_fusion_below_direct_hits():
    from mem_ol.services.pre_turn import _fuse_entities, _relation_candidates

    edges = [
        {
            "source_id": "e-yaolao",
            "source_name": "药老",
            "relation_label": "收为徒弟",
            "target_id": "e-xiaoyan",
            "target_name": "萧炎",
            "score": 3.0,
        }
    ]
    relation = _relation_candidates(edges)
    assert {item["canonical_name"] for item in relation} == {"药老", "萧炎"}
    assert {item["route"] for item in relation} == {"relation"}

    fused = _fuse_entities(
        [{"entity_id": "e-xiaoyan", "canonical_name": "萧炎"}],
        [{"entity_id": "e-yaolao", "canonical_name": "药老"}],
        relation,
    )
    # 名称直接命中的两个实体排在仅靠关系标签补进来的实体之前。
    assert fused[0]["entity_id"] == "e-xiaoyan"
    assert fused[1]["entity_id"] == "e-yaolao"
