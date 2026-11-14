from mem_ol.config import Settings
from mem_ol.domain import GMResult
from mem_ol.services.dialogue import DialogueEngine


class FakeStore:
    def get_recent_turns(self, thread_id, turns):
        return []


class FakeLLM:
    def __init__(self):
        self.messages = None

    def chat_json_model(self, model_cls, messages, **kwargs):
        assert model_cls is GMResult
        self.messages = messages
        return GMResult(
            plan={
                "intent": "action",
                "expected_outcome": "推门成功",
                "continuity": "承接守卫",
                "state_change": "进入内院",
            },
            effects=[
                {
                    "kind": "scale_delta",
                    "target": "守卫",
                    "field": "alert",
                    "value": 10,
                    "reason": "听到门响",
                }
            ],
            reply="你推门进去，守卫没有回头。",
        )


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
    assert result["effects"][0]["kind"] == "scale_delta"
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
    assert '"reply"' in user_content
    assert "我推门进去" in user_content
