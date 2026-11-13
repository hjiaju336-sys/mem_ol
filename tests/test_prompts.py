from mem_ol.services.llm import _extract_json
from mem_ol.services.prompts import load_prompt, render_prompt


def test_api2_context_prompt_renders():
    system, user = load_prompt("api2_context.md")
    assert "context_summary" in system
    assert "rewritten_query" in system
    rendered = render_prompt(
        user,
        previous_turn="[玩家] 攻击他。\n[GM] 老猴从横枝上跳开。",
        user_message="继续追",
    )
    assert "$previous_turn" not in rendered
    assert "继续追" in rendered


def test_api2_entities_prompt_renders():
    system, user = load_prompt("api2_entities.md")
    assert "entities" in system
    rendered = render_prompt(
        user,
        user_message="攻击他",
        recent_context="[GM] 老猴蹲在横枝上盯着你。",
    )
    assert "$user_message" not in rendered
    assert "攻击他" in rendered


def test_entity_commit_prompt_renders():
    system, user = load_prompt("entity_commit.md")
    assert "实体与关系固化器" in system
    rendered = render_prompt(
        user,
        user_message="我走进水帘洞",
        gm_reply="洞内有人。",
        hypothetical_entities_json="[]",
        entity_candidates_json="[]",
        context_snippets="（无）",
    )
    assert "$gm_reply" not in rendered
    assert "洞内有人" in rendered


def test_extract_json_from_code_fence():
    assert _extract_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_repairs_truncated_output():
    assert _extract_json('{"a": 1, "b": [1, 2') == {"a": 1, "b": [1, 2]}
