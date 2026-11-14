from mem_ol.services.llm import _extract_json
from mem_ol.services.prompts import (
    load_prompt,
    load_prompt_sections,
    load_system_prompt,
    render_prompt,
)


def test_api2_context_prompt_renders():
    system, user = load_prompt("api2_context.md")
    assert "context_summary" in system
    assert "rewritten_query" in system
    assert "channel" in system
    assert "directives" in system
    rendered = render_prompt(
        user,
        previous_turn="[玩家] 攻击他。\n[GM] 老猴从横枝上跳开。",
        user_message="继续追",
        pending_instructions="- 写短点",
    )
    assert "$previous_turn" not in rendered
    assert "继续追" in rendered
    assert "写短点" in rendered


def test_gm_core_and_contracts_load():
    core = load_system_prompt("gm_core.md")
    assert "第二人称" in core
    contracts = load_prompt_sections("gm_contracts.md")
    assert {"action", "investigate", "social", "combat", "daily", "cultivation", "inquiry", "default"} <= set(contracts)
    assert "必须出现" in contracts["action"]


def test_gm_output_prompt_contains_effect_schema():
    text = load_system_prompt("gm_output.md")
    assert "effects" in text
    assert "reply" in text
    assert "hp_delta" in text
    assert "time_advance" in text
    assert "重复" in text


def test_gm_meta_prompt_renders():
    system, user = load_prompt("gm_meta.md")
    assert "不写故事" in system
    rendered = render_prompt(
        user,
        question="你是什么模型",
        recent_context="[玩家] 你好",
    )
    assert "你是什么模型" in rendered
    assert "$question" not in rendered


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


def test_relation_prompts_use_free_label():
    for name in ("world_import.md", "entity_commit.md", "world_consolidate.md"):
        system, user = load_prompt(name)
        text = system + user
        assert "relation_label" in text
        assert "自由" in text or "不要" in text


def test_prompts_allow_reifying_events():
    world, _ = load_prompt("world_import.md")
    commit, _ = load_prompt("entity_commit.md")
    assert "婚约" in world and "实体化" in world
    assert "婚约" in commit and "event" in commit


def test_extract_json_from_code_fence():
    assert _extract_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_repairs_truncated_output():
    assert _extract_json('{"a": 1, "b": [1, 2') == {"a": 1, "b": [1, 2]}
