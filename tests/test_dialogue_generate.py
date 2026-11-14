from mem_ol.config import Settings
from mem_ol.domain import StateJudgeResult
from mem_ol.services.dialogue import DialogueEngine
from mem_ol.services.state_judge import StateJudge


class FakeStore:
    def get_recent_turns(self, thread_id, turns):
        return []


class FakeLLM:
    def __init__(self):
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        return "你推门进去，守卫没有回头。"


def test_generate_composes_prompt_and_returns_reply():
    engine = DialogueEngine.__new__(DialogueEngine)
    engine.store = FakeStore()
    engine.settings = Settings()
    engine.llm = FakeLLM()
    engine.retrieval = None

    packet = {
        "trace_id": "trace-1",
        "intent": "action",
        "directives": {"length": "short", "options": "none"},
        "context_summary": "守卫停在门外。",
        "entity_candidates": [],
        "context_candidates": [],
        "graph_paths": [],
        "plot_context": {},
    }
    result = engine.generate(
        "thread-1",
        "我推门进去",
        pre_turn=packet,
        turn_no=1,
        client_context={
            "custom_prompt": "用偏古风的叙述。",
            "pending_instructions": ["多写心理"],
            "lorebook": {
                "entries": [
                    {
                        "uid": 0,
                        "key": ["门"],
                        "content": "这扇门是青铜做的。",
                        "constant": True,
                    }
                ]
            },
        },
        state_view={
            "player": {
                "hp": 17,
                "max_hp": 20,
                "attack": 5,
                "attack_bonus": 2,
                "defense": 2,
                "tags": ["受伤"],
            },
            "targets": {},
            "world": {"time_label": "第1年 1月1日 08:00", "flags": {}},
        },
        resolution={
            "outcome": "success",
            "rolls": [{"name": "攻击", "formula": "1d20+7", "total": 18, "dc": 11}],
            "facts": ["攻击命中守卫"],
            "deltas": [{"path": "actors.npc.hp", "before": 15, "after": 7}],
        },
    )

    assert result["reply"] == "你推门进去，守卫没有回头。"
    assert result["intent"] == "action"
    assert result["lorebook_active"] == ["0"]
    assert "effects" not in result
    system_prompt = engine.llm.messages[0]["content"]
    assert "用偏古风的叙述" in system_prompt
    assert "这扇门是青铜做的" in system_prompt
    assert "本轮契约：行动" in system_prompt
    assert "HP 17/20" in system_prompt
    assert "时间：第1年 1月1日 08:00" in system_prompt
    assert "攻击命中守卫" in system_prompt
    user_content = engine.llm.messages[-1]["content"]
    assert "篇幅短" in user_content
    assert "不要给选项" in user_content
    assert "多写心理" in user_content
    assert "数值权威" in user_content
    assert "只输出故事正文" in user_content
    assert "我推门进去" in user_content


class FakeJudgeLLM:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def chat_json_model(self, model_cls, messages, **kwargs):
        assert model_cls is StateJudgeResult
        self.messages = messages
        return self.result


def test_state_judge_proposes_effects_and_drops_invalid_ones():
    llm = FakeJudgeLLM(
        StateJudgeResult(
            effects=[
                {"kind": "hp_delta", "target": "player", "value": -3},
                {"kind": "scale_add", "target": "player", "value": 5},
            ],
            appearances=["守卫"],
        )
    )
    judge = StateJudge(llm=llm, settings=Settings())
    decision = judge.propose(
        user_action="我推门进去",
        reply="守卫反手一剑，你左臂中招。",
        state_view={
            "player": {
                "hp": 20,
                "max_hp": 20,
                "attack": 5,
                "attack_bonus": 2,
                "defense": 2,
                "tags": [],
            },
            "targets": {},
            "world": {"time_label": "第1年 1月1日 08:00", "flags": {}},
        },
    )
    assert [item["kind"] for item in decision["effects"]] == ["hp_delta"]
    assert decision["appearances"] == ["守卫"]
    user_prompt = llm.messages[-1]["content"]
    assert "守卫反手一剑" in user_prompt
    assert "HP 20/20" in user_prompt

    # 裁决失败时返回空决策，不阻塞正文。
    class BrokenLLM:
        def chat_json_model(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert StateJudge(llm=BrokenLLM(), settings=Settings()).propose(
        user_action="推门", reply="你推门进去。"
    ) == {"effects": [], "appearances": []}


class FakeRetrieval:
    def retrieve(self, thread_id, query, k=None):
        return [
            {
                "chunk_no": "B0001-P001-C001",
                "parent_no": "B0001-P001",
                "content": "门后是一片寂静的院子。",
                "score": 0.9,
            }
        ]


def test_generate_without_pre_turn_keeps_client_context_and_state():
    # API2 失败退回基础链路时，自定义提示词/世界书/状态卡不能丢。
    engine = DialogueEngine.__new__(DialogueEngine)
    engine.store = FakeStore()
    engine.settings = Settings()
    engine.llm = FakeLLM()
    engine.retrieval = FakeRetrieval()

    engine.generate(
        "thread-1",
        "我推门进去",
        pre_turn=None,
        turn_no=1,
        client_context={
            "custom_prompt": "用偏古风的叙述。",
            "lorebook": {
                "entries": [
                    {
                        "uid": 0,
                        "key": ["门"],
                        "content": "这扇门是青铜做的。",
                        "constant": True,
                    }
                ]
            },
        },
        state_view={
            "player": {
                "hp": 17,
                "max_hp": 20,
                "attack": 5,
                "attack_bonus": 2,
                "defense": 2,
                "tags": [],
            },
            "targets": {},
            "world": {"time_label": "第1年 1月1日 08:00", "flags": {}},
        },
    )

    system_prompt = engine.llm.messages[0]["content"]
    assert "用偏古风的叙述" in system_prompt
    assert "这扇门是青铜做的" in system_prompt
    assert "HP 17/20" in system_prompt
    assert "门后是一片寂静的院子" in system_prompt
