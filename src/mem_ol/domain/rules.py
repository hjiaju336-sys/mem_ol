from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mem_ol.log import get_logger

logger = get_logger(__name__)

_DEFAULT_RULES = Path(__file__).resolve().parents[1] / "rules" / "default.json"


class StateRules(BaseModel):
    player: dict[str, Any]
    target: dict[str, Any]
    starter_items: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AttackRules(BaseModel):
    dc_base: int = 10
    counter_dice: str = "1d3"
    damage_floor: int = 1


class ScaleRules(BaseModel):
    min: int = 0
    max: int = 100
    delta_limit: int = 50


class EffectRules(BaseModel):
    max_per_turn: int = 8
    hp_delta_limit: int = 20
    scale: ScaleRules = Field(default_factory=ScaleRules)


class TimeRules(BaseModel):
    months_per_year: int = 12
    days_per_month: int = 30
    hours_per_day: int = 24
    minutes_per_hour: int = 60
    start: dict[str, int] = Field(
        default_factory=lambda: {
            "year": 1,
            "month": 1,
            "day": 1,
            "hour": 8,
            "minute": 0,
        }
    )


class RulePack(BaseModel):
    schema_version: int = 1
    state: StateRules
    attack: AttackRules = Field(default_factory=AttackRules)
    effects: EffectRules = Field(default_factory=EffectRules)
    time: TimeRules = Field(default_factory=TimeRules)


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> RulePack:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pack = RulePack.model_validate(data)
    logger.info(
        "规则包加载完成",
        extra={
            "event": "rules.loaded",
            "path": path,
            "schema_version": pack.schema_version,
        },
    )
    return pack


def get_rules() -> RulePack:
    """按文件 mtime 热加载；改 rules/default.json 后下一轮生效。"""
    path = Path(os.getenv("MEM_OL_RULES_PATH", str(_DEFAULT_RULES)))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        raise RuntimeError(f"规则包不存在: {path}") from None
    return _load(str(path), mtime)
