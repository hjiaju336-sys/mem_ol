from mem_ol.config import Settings
from mem_ol.services.dialogue import DialogueEngine


def _engine() -> DialogueEngine:
    engine = DialogueEngine.__new__(DialogueEngine)
    engine.settings = Settings()
    return engine


def test_prompt_contains_custom_prompt_and_lorebook():
    engine = _engine()
    prompt, active = engine._build_system_prompt_from_packet(
        {
            "intent": "investigate",
            "entity_candidates": [],
            "context_candidates": [],
            "graph_paths": [
                {
                    "nodes": [
                        {"entity_id": "a", "name": "药老"},
                        {"entity_id": "b", "name": "萧炎"},
                    ],
                    "edges": [
                        {
                            "start_id": "a",
                            "end_id": "b",
                            "rel_id": "r1",
                            "relation_label": "收为徒弟",
                        }
                    ],
                }
            ],
            "plot_context": {},
        },
        client_context={
            "custom_prompt": "用偏古风的叙述。",
            "lorebook": {
                "entries": [
                    {
                        "uid": 0,
                        "key": ["水帘洞"],
                        "content": "水帘洞在花果山深处。",
                        "constant": True,
                    }
                ]
            },
            "pending_instructions": [],
        },
        user_text="查看水帘洞",
        history_texts=[],
    )
    assert "用偏古风的叙述" in prompt
    assert "水帘洞在花果山深处" in prompt
    assert "本轮契约：调查" in prompt
    assert "药老 -[收为徒弟]-> 萧炎" in prompt
    assert active == ["0"]


def test_turn_instruction_formats_directives_and_pending():
    engine = _engine()
    text = engine._build_turn_instruction(
        intent="action",
        directives={"length": "short", "options": "none", "pace": "fast"},
        packet={"context_summary": "守卫停在门外。"},
        client_context={"pending_instructions": ["多写心理"]},
    )
    assert "篇幅短" in text
    assert "不要给选项" in text
    assert "节奏快" in text
    assert "多写心理" in text
    assert "守卫停在门外" in text
    assert "不得在正文中确认" in text
