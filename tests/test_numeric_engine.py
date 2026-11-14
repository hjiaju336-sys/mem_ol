import random

from mem_ol.services.numeric_engine import (
    advance_time,
    apply_effects,
    ensure_state,
    format_time,
    new_game_state,
    resolve_action,
    roll,
    state_view,
)


class FixedRng:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        return self.values.pop(0)


def test_new_state_has_player_and_weapon():
    state = ensure_state(new_game_state())
    assert state["actors"]["player"]["hp"] == 20
    assert state["items"]["item:starter_sword"]["stats"]["attack_bonus"] == 2
    view = state_view(state)
    assert view["player"]["attack"] == 5
    assert view["player"]["attack_bonus"] == 2


def test_roll_parses_dice():
    assert roll("1d6", FixedRng([4])) == 4
    assert roll("2d4+1", FixedRng([2, 3])) == 6
    assert roll("bad", FixedRng([])) == 0


def test_attack_success_applies_damage():
    state = new_game_state()
    action = {
        "kind": "attack",
        "target": "守卫",
        "approach": "从侧面劈砍",
        "needs_check": True,
    }
    state, resolution = resolve_action(
        state,
        action,
        thread_id="thread-1",
        turn_no=1,
        rng=FixedRng([20, 4]),
    )
    assert resolution is not None
    assert resolution["outcome"] == "success"
    assert resolution["rolls"][0]["total"] == 27
    assert state["actors"]["npc:守卫"]["hp"] == 5
    assert resolution["deltas"][0]["after"] == 5
    assert "受伤" in state["actors"]["npc:守卫"]["tags"]


def test_attack_failure_counterattacks():
    state = new_game_state()
    action = {
        "kind": "attack",
        "target": "守卫",
        "approach": "正面劈砍",
        "needs_check": True,
    }
    state, resolution = resolve_action(
        state,
        action,
        thread_id="thread-1",
        turn_no=1,
        rng=FixedRng([1, 2]),
    )
    assert resolution["outcome"] == "failure"
    assert state["actors"]["player"]["hp"] == 18
    assert resolution["deltas"][0]["path"] == "actors.player.hp"


def test_calendar_advances_days_months_and_years():
    state = new_game_state()
    assert format_time(state["world"]["time"]) == "第1年 1月1日 08:00"

    advance_time(state, days=1)
    assert format_time(state["world"]["time"]) == "第1年 1月2日 08:00"

    advance_time(state, days=29)
    assert format_time(state["world"]["time"]) == "第1年 2月1日 08:00"

    advance_time(state, days=12 * 30 - 30)
    assert state["world"]["time"]["year"] == 2


def test_time_advance_effect_uses_engine_calendar():
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state,
        [
            {
                "kind": "time_advance",
                "value": {"days": 1, "hours": 2, "minutes": 30},
                "reason": "第二天清晨",
            }
        ],
    )
    assert not rejected
    assert applied[0]["path"] == "world.time"
    assert format_time(state["world"]["time"]) == "第1年 1月2日 10:30"


def test_apply_effects_updates_tags_scales_and_flags():
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state,
        [
            {
                "kind": "tag_add",
                "target": "player",
                "value": "tracked",
                "reason": "被盯上",
            },
            {
                "kind": "scale_delta",
                "target": "守卫",
                "field": "alert",
                "value": 25,
                "reason": "听到动静",
            },
            {
                "kind": "flag_set",
                "target": "world",
                "field": "alarm",
                "value": True,
                "reason": "警铃",
            },
            {
                "kind": "hp_delta",
                "target": "player",
                "value": -3,
                "reason": "摔伤",
            },
        ],
    )
    assert not rejected
    assert "tracked" in state["actors"]["player"]["tags"]
    assert state["actors"]["npc:守卫"]["scales"]["alert"] == 25
    assert state["world"]["flags"]["alarm"] is True
    assert state["actors"]["player"]["hp"] == 17
    assert len(applied) == 4


def test_apply_effects_parses_string_boolean():
    state = new_game_state()
    state, _, _ = apply_effects(
        state,
        [{"kind": "flag_set", "field": "alarm", "value": "false"}],
    )
    assert state["world"]["flags"]["alarm"] is False


def test_apply_effects_rejects_duplicate_resolution_damage():
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state,
        [{"kind": "hp_delta", "target": "player", "value": -3}],
        blocked_paths={"actors.player.hp"},
    )
    assert applied == []
    assert rejected[0]["error"] == "duplicate_resolution_delta"
    assert state["actors"]["player"]["hp"] == 20


def test_apply_effects_clamps_scale_and_hp():
    state = new_game_state()
    state["actors"]["player"]["scales"]["fatigue"] = 90
    state, _, _ = apply_effects(
        state,
        [
            {"kind": "scale_delta", "target": "player", "field": "fatigue", "value": 999},
            {"kind": "hp_delta", "target": "player", "value": -999},
        ],
    )
    assert state["actors"]["player"]["scales"]["fatigue"] == 100
    assert state["actors"]["player"]["hp"] == 0


def test_non_attack_has_no_resolution():
    state = new_game_state()
    action = {"kind": "social", "target": "守卫", "needs_check": False}
    state, resolution = resolve_action(
        state,
        action,
        thread_id="thread-1",
        turn_no=1,
        rng=random.Random(1),
    )
    assert resolution is None
    assert state["actors"]["player"]["hp"] == 20


def test_flag_set_requires_field():
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state, [{"kind": "flag_set", "value": True}]
    )
    assert applied == []
    assert rejected[0]["error"] == "empty_flag"
    assert not state["world"].get("flags")


def test_tag_no_op_is_rejected_and_empty_time_advance_is_allowed():
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state,
        [
            {"kind": "tag_remove", "target": "player", "value": "不存在"},
            # 零时长推进是合法的（时间静止等剧情），引擎不改时间但不算失败。
            {"kind": "time_advance", "value": {}},
        ],
    )
    assert [item["path"] for item in applied] == ["world.time"]
    assert rejected[0]["error"] == "no_change"
    assert state["world"]["time"]["day"] == 1


def test_new_npc_target_creates_actor():
    # 新出现的 NPC 允许登记，后续轮次才能继续扣血/加状态。
    state = new_game_state()
    state, applied, rejected = apply_effects(
        state, [{"kind": "scale_delta", "target": "守卫", "field": "alert", "value": 10}]
    )
    assert not rejected
    assert state["actors"]["npc:守卫"]["scales"]["alert"] == 10


def test_state_view_exposes_entity_id():
    state = new_game_state()
    state["actors"]["npc:守卫"] = {"name": "守卫", "entity_id": "e-guard"}
    view = state_view(state)
    assert view["targets"]["npc:守卫"]["entity_id"] == "e-guard"
