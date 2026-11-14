from __future__ import annotations

from typing import Any


def format_state_card(view: dict[str, Any]) -> str:
    """把 state_view 渲染成状态卡；GM 与状态裁决器共用同一份文本。"""
    player = view.get("player") or {}
    if not player:
        return ""
    tags = "、".join(player.get("tags") or []) or "无"
    player_scales = "、".join(
        f"{key}={value}" for key, value in (player.get("scales") or {}).items()
    ) or "无"
    world = view.get("world") or {}
    time_label = world.get("time_label")
    lines = [
        "【当前状态（权威）】",
        f"时间：{time_label or '未记录'}",
        f"你：HP {player.get('hp', 0)}/{player.get('max_hp', 0)}，"
        f"攻击 {player.get('attack', 0)}"
        f"+{player.get('attack_bonus', 0)}，"
        f"防御 {player.get('defense', 0)}；状态：{tags}；"
        f"数值：{player_scales}",
    ]
    for target in (view.get("targets") or {}).values():
        target_tags = "、".join(target.get("tags") or []) or "无"
        target_scales = "、".join(
            f"{key}={value}"
            for key, value in (target.get("scales") or {}).items()
        ) or "无"
        lines.append(
            f"{target.get('name', '对手')}："
            f"HP {target.get('hp', 0)}/{target.get('max_hp', 0)}；"
            f"状态：{target_tags}；数值：{target_scales}"
        )
    flags = world.get("flags") or {}
    if flags:
        lines.append(
            "世界标记：" + "、".join(f"{key}={value}" for key, value in flags.items())
        )
    return "\n".join(lines)


def format_resolution(resolution: dict[str, Any]) -> str:
    if not resolution:
        return ""
    outcome = {
        "success": "成功",
        "failure": "失败",
        "partial": "部分成功",
        "critical_success": "大成功",
        "critical_failure": "大失败",
        "no_check": "无需检定",
    }.get(str(resolution.get("outcome")), str(resolution.get("outcome")))
    lines = [f"【本轮结算（权威，不得改写）】结果：{outcome}"]
    for roll in resolution.get("rolls") or []:
        lines.append(
            f"{roll.get('name', '检定')}：{roll.get('formula', '')}"
            f"={roll.get('total', '')} vs DC {roll.get('dc', '')}"
        )
    facts = resolution.get("facts") or []
    if facts:
        lines.append("必须发生：" + "；".join(str(item) for item in facts))
    deltas = resolution.get("deltas") or []
    if deltas:
        changes = [
            f"{item.get('path')} {item.get('before')}→{item.get('after')}"
            for item in deltas
        ]
        lines.append("数值变化：" + "；".join(changes))
    lines.append(
        "必须按以上结果叙述，不得自行掷骰、修改数值或新增未结算结果。"
    )
    return "\n".join(lines)
