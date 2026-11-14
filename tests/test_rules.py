import json

from mem_ol.domain.rules import get_rules
from mem_ol.services.numeric_engine import new_game_state


def test_default_rule_pack_loads():
    rules = get_rules()
    assert rules.state.player["hp"] == 20
    assert rules.attack.dc_base == 10
    assert rules.effects.scale.max == 100
    assert "item:starter_sword" in rules.state.starter_items


def test_rule_pack_can_be_overridden(monkeypatch, tmp_path):
    source = get_rules().model_dump()
    source["state"]["player"]["hp"] = 30
    source["state"]["player"]["max_hp"] = 30
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MEM_OL_RULES_PATH", str(path))

    rules = get_rules()
    state = new_game_state()
    assert rules.state.player["hp"] == 30
    assert state["actors"]["player"]["hp"] == 30
