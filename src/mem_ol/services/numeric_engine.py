from __future__ import annotations

import copy
import hashlib
import random
import re
from typing import Any

from mem_ol.domain.rules import get_rules

_DICE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$", re.IGNORECASE)


def _default_player() -> dict[str, Any]:
    return copy.deepcopy(get_rules().state.player)


def _default_target(name: str) -> dict[str, Any]:
    target = copy.deepcopy(get_rules().state.target)
    target["name"] = name
    return target


def _default_time() -> dict[str, int]:
    return copy.deepcopy(get_rules().time.start)


def format_time(value: dict[str, int] | None) -> str:
    time = _default_time()
    if isinstance(value, dict):
        time.update({key: int(value.get(key, time[key])) for key in time})
    return (
        f"第{time['year']}年 {time['month']}月{time['day']}日 "
        f"{time['hour']:02d}:{time['minute']:02d}"
    )


def advance_time(
    state: dict[str, Any],
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
) -> dict[str, Any]:
    """按规则包历法推进时间；月/年按固定天数滚动。"""
    ensure_state(state)
    rules = get_rules().time
    current = state["world"]["time"]
    total = (
        (int(current.get("year", 1)) - 1)
        * rules.months_per_year
        * rules.days_per_month
        + (int(current.get("month", 1)) - 1) * rules.days_per_month
        + (int(current.get("day", 1)) - 1)
    ) * rules.hours_per_day * rules.minutes_per_hour
    total += int(current.get("hour", 0)) * rules.minutes_per_hour
    total += int(current.get("minute", 0))
    total += (int(days) * rules.hours_per_day + int(hours)) * rules.minutes_per_hour
    total += int(minutes)
    total = max(0, total)

    day_minutes = rules.hours_per_day * rules.minutes_per_hour
    total_days, rest = divmod(total, day_minutes)
    hour, minute = divmod(rest, rules.minutes_per_hour)
    year, day_of_year = divmod(total_days, rules.months_per_year * rules.days_per_month)
    month, day = divmod(day_of_year, rules.days_per_month)
    state["world"]["time"] = {
        "year": year + 1,
        "month": month + 1,
        "day": day + 1,
        "hour": hour,
        "minute": minute,
    }
    return state


def _duration_parts(value: Any) -> tuple[int, int, int]:
    if isinstance(value, dict):
        return (
            int(value.get("days", 0) or 0),
            int(value.get("hours", 0) or 0),
            int(value.get("minutes", 0) or 0),
        )
    try:
        return 0, 0, int(value or 0)
    except (TypeError, ValueError):
        return 0, 0, 0


def new_game_state() -> dict[str, Any]:
    rules = get_rules()
    return {
        "schema_version": rules.schema_version,
        "actors": {"player": _default_player()},
        "items": copy.deepcopy(rules.state.starter_items),
        "world": {"time": _default_time()},
    }


def ensure_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """补齐最小角色/武器，保证第一次攻击就能跑。"""
    current = state if isinstance(state, dict) else {}
    actors = current.setdefault("actors", {})
    player = actors.setdefault("player", _default_player())
    for key, value in _default_player().items():
        player.setdefault(key, value)
    player.setdefault("scales", {"fatigue": 0})
    items = current.setdefault("items", {})
    weapon_id = (player.get("equipment") or {}).get("weapon")
    if weapon_id and weapon_id not in items:
        starter = get_rules().state.starter_items.get(weapon_id)
        if starter:
            items[weapon_id] = copy.deepcopy(starter)
    world = current.setdefault("world", {})
    world.setdefault("flags", {})
    world.setdefault("time", _default_time())
    current.setdefault("schema_version", 1)
    return current


def turn_rng(thread_id: str, turn_no: int) -> random.Random:
    seed = int.from_bytes(
        hashlib.sha256(f"{thread_id}:{turn_no}".encode("utf-8")).digest()[:8],
        "big",
    )
    return random.Random(seed)


def roll(expr: str, rng: random.Random) -> int:
    """支持 1d6 / d20 / 2d4+1；非法表达式按 0 处理。"""
    match = _DICE.match(str(expr or "").strip())
    if not match:
        return 0
    count = int(match.group(1) or 1)
    sides = max(1, int(match.group(2)))
    modifier = int(match.group(3) or 0)
    return sum(rng.randint(1, sides) for _ in range(max(1, count))) + modifier


def _target_id(name: str) -> str:
    cleaned = re.sub(r"\s+", "_", (name or "").strip())[:40]
    return cleaned if cleaned.startswith("npc:") else f"npc:{cleaned or '对手'}"


def _weapon_stats(state: dict[str, Any]) -> dict[str, Any]:
    player = state["actors"]["player"]
    weapon_id = (player.get("equipment") or {}).get("weapon")
    item = (state.get("items") or {}).get(weapon_id)
    return (item or {}).get("stats") or {}


def state_view(state: dict[str, Any]) -> dict[str, Any]:
    ensure_state(state)
    player = state["actors"]["player"]
    weapon = _weapon_stats(state)
    players = {
        "hp": int(player.get("hp", 0)),
        "max_hp": int(player.get("max_hp", 0)),
        "attack": int(player.get("attack", 0)),
        "attack_bonus": int(weapon.get("attack_bonus", 0)),
        "defense": int(player.get("defense", 0)),
        "tags": list(player.get("tags", [])),
        "scales": dict(player.get("scales") or {}),
    }
    targets = {
        actor_id: {
            "name": actor.get("name") or actor_id,
            "hp": int(actor.get("hp", 0)),
            "max_hp": int(actor.get("max_hp", 0)),
            "tags": list(actor.get("tags", [])),
            "scales": dict(actor.get("scales") or {}),
        }
        for actor_id, actor in state["actors"].items()
        if actor_id != "player"
    }
    world = state.get("world") or {}
    return {
        "player": players,
        "targets": targets,
        "world": {
            "flags": dict(world.get("flags") or {}),
            "time": dict(world.get("time") or {}),
            "time_label": format_time(world.get("time")),
        },
    }


def resolve_action(
    state: dict[str, Any],
    action: dict[str, Any],
    *,
    thread_id: str,
    turn_no: int,
    rng: random.Random | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """只实现 attack 的最小闭环；其他行为交给 GM 叙述。"""
    ensure_state(state)
    kind = str(action.get("kind") or "other")
    if kind != "attack" or not action.get("needs_check"):
        return state, None

    player = state["actors"]["player"]
    target_name = str(action.get("target") or "").strip() or "对手"
    target_id = _target_id(target_name)
    target = state["actors"].setdefault(
        target_id, _default_target(target_name)
    )

    rules = get_rules()
    weapon = _weapon_stats(state)
    attack_bonus = int(player.get("attack", 0)) + int(
        weapon.get("attack_bonus", 0)
    )
    rng = rng or turn_rng(thread_id, turn_no)
    attack_roll = rng.randint(1, 20)
    total = attack_roll + attack_bonus
    dc = rules.attack.dc_base + int(target.get("defense", 0))
    success = total >= dc

    deltas: list[dict[str, Any]] = []
    facts: list[str] = []
    if success:
        raw_damage = roll(str(weapon.get("damage_dice") or "1d4"), rng)
        damage = max(
            rules.attack.damage_floor,
            raw_damage + max(0, attack_bonus - int(target.get("defense", 0))),
        )
        before = int(target.get("hp", 0))
        after = max(0, before - damage)
        target["hp"] = after
        if "受伤" not in target.setdefault("tags", []):
            target["tags"].append("受伤")
        if after <= 0 and "倒下" not in target["tags"]:
            target["tags"].append("倒下")
        deltas.append(
            {"path": f"actors.{target_id}.hp", "before": before, "after": after}
        )
        facts.append(f"攻击命中{target_name}，造成 {damage} 点伤害")
        if after <= 0:
            facts.append(f"{target_name}倒下")
    else:
        counter = roll(rules.attack.counter_dice, rng)
        before = int(player.get("hp", 0))
        after = max(0, before - counter)
        player["hp"] = after
        if "受伤" not in player.setdefault("tags", []):
            player["tags"].append("受伤")
        deltas.append(
            {"path": "actors.player.hp", "before": before, "after": after}
        )
        facts.append(f"攻击落空，{target_name}反击造成 {counter} 点伤害")

    resolution = {
        "action": {
            "kind": kind,
            "target": target_name,
            "approach": action.get("approach") or "",
        },
        "outcome": "success" if success else "failure",
        "rolls": [
            {
                "name": "攻击",
                "formula": f"1d20+{attack_bonus}",
                "roll": attack_roll,
                "total": total,
                "dc": dc,
            }
        ],
        "deltas": deltas,
        "facts": facts,
        "state": state_view(state),
    }
    return state, resolution


def _actor_id_for(state: dict[str, Any], name: str) -> str:
    cleaned = (name or "").strip()
    if cleaned in ("", "player", "你", "玩家"):
        return "player"
    for actor_id, actor in state["actors"].items():
        if actor.get("name") == cleaned:
            return actor_id
    return _target_id(cleaned)


def apply_effects(
    state: dict[str, Any],
    effects: list[dict[str, Any]],
    *,
    blocked_paths: set[str] | None = None,
    max_effects: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """把 GM 提出的剧情后果校验后写入状态。

    返回 (state, applied, rejected)。blocked_paths 用于防止重复结算
    （例如本轮攻击已经扣过 HP，GM 再次提 hp_delta）。
    """
    rules = get_rules()
    limit = max_effects if max_effects is not None else rules.effects.max_per_turn
    ensure_state(state)
    blocked = blocked_paths or set()
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for effect in (effects or [])[:limit]:
        kind = str(effect.get("kind") or "")
        target_name = str(effect.get("target") or "")
        field = str(effect.get("field") or "").strip()
        value = effect.get("value")
        reason = str(effect.get("reason") or "")

        if kind in ("hp_delta", "tag_add", "tag_remove", "scale_delta"):
            actor_id = _actor_id_for(state, target_name)
            actor = state["actors"].setdefault(
                actor_id, _default_target(target_name or actor_id)
            )
            actor.setdefault("tags", [])
            actor.setdefault("scales", {})
        else:
            actor_id = "world"
            actor = {}

        if kind == "hp_delta":
            path = f"actors.{actor_id}.hp"
            if path in blocked:
                rejected.append({**effect, "error": "duplicate_resolution_delta"})
                continue
            try:
                delta = int(value)
            except (TypeError, ValueError):
                rejected.append({**effect, "error": "invalid_value"})
                continue
            delta = max(
                -rules.effects.hp_delta_limit,
                min(rules.effects.hp_delta_limit, delta),
            )
            before = int(actor.get("hp", 0))
            after = max(0, min(int(actor.get("max_hp", before)), before + delta))
            actor["hp"] = after
            applied.append({"path": path, "before": before, "after": after, "reason": reason})
        elif kind in ("tag_add", "tag_remove"):
            tag = str(value or field).strip()
            if not tag:
                rejected.append({**effect, "error": "empty_tag"})
                continue
            if kind == "tag_add" and tag not in actor["tags"]:
                actor["tags"].append(tag)
            if kind == "tag_remove" and tag in actor["tags"]:
                actor["tags"].remove(tag)
            applied.append({"path": f"actors.{actor_id}.tags", "value": tag, "op": kind, "reason": reason})
        elif kind == "flag_set":
            flag = field or str(value or "").strip()
            if not flag:
                rejected.append({**effect, "error": "empty_flag"})
                continue
            flag_value = (
                value.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(value, str)
                else bool(value)
            )
            state["world"].setdefault("flags", {})[flag] = flag_value
            applied.append(
                {"path": f"world.flags.{flag}", "value": flag_value, "reason": reason}
            )
        elif kind == "scale_delta":
            if not field:
                rejected.append({**effect, "error": "empty_scale"})
                continue
            try:
                delta = int(value)
            except (TypeError, ValueError):
                rejected.append({**effect, "error": "invalid_value"})
                continue
            scale_rules = rules.effects.scale
            delta = max(
                -scale_rules.delta_limit,
                min(scale_rules.delta_limit, delta),
            )
            before = int(actor["scales"].get(field, 0))
            after = max(
                scale_rules.min,
                min(scale_rules.max, before + delta),
            )
            actor["scales"][field] = after
            applied.append({"path": f"actors.{actor_id}.scales.{field}", "before": before, "after": after, "reason": reason})
        elif kind == "time_advance":
            days, hours, minutes = _duration_parts(value)
            before = format_time(state["world"].get("time"))
            advance_time(state, days=days, hours=hours, minutes=minutes)
            after = format_time(state["world"].get("time"))
            applied.append(
                {
                    "path": "world.time",
                    "before": before,
                    "after": after,
                    "days": days,
                    "hours": hours,
                    "minutes": minutes,
                    "reason": reason,
                }
            )
        else:
            rejected.append({**effect, "error": "unsupported_kind"})

    return state, applied, rejected
