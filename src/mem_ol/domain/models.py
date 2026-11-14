from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mem_ol.domain.enums import (
    ACTION_KINDS,
    CHANNELS,
    DIRECTIVE_VALUES,
    INTENTS,
    META_KINDS,
    EntityType,
)
from mem_ol.log import get_logger

logger = get_logger(__name__)


def normalize_relation_label(value: Any) -> str:
    """关系标签自由文本；只做空白/全半角归一，不做语义限制。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:80] or "未明确"


def _normalize_choice(
    value: Any, allowed: set[str], default: str | None
) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


class ActionProposal(BaseModel):
    """LLM 只填意图与对象，数值由引擎决定。"""

    kind: str = "other"
    target: str = Field(default="", max_length=200)
    approach: str = Field(default="", max_length=500)
    needs_check: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "ActionProposal":
        self.kind = _normalize_choice(self.kind, ACTION_KINDS, "other") or "other"
        return self


class TurnDirective(BaseModel):
    length: str | None = None
    pace: str | None = None
    focus: str | None = None
    options: str | None = None
    tone: str | None = None
    notes: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def normalize(self) -> "TurnDirective":
        for name, allowed in DIRECTIVE_VALUES.items():
            setattr(
                self, name, _normalize_choice(getattr(self, name), allowed, None)
            )
        return self

    def is_empty(self) -> bool:
        return not any(
            getattr(self, name) for name in DIRECTIVE_VALUES
        ) and not self.notes.strip()


class AP2ContextResult(BaseModel):
    channel: str = "narrative"
    intent: str = "default"
    context_summary: str = Field(default="", max_length=300)
    rewritten_query: str = Field(default="", max_length=800)
    narrative_query: str = Field(default="", max_length=1200)
    directives: TurnDirective = Field(default_factory=TurnDirective)
    action: ActionProposal = Field(default_factory=ActionProposal)
    meta_kind: str = "instruction"
    meta_question: str = Field(default="", max_length=800)

    @field_validator("channel", "intent", "meta_kind", mode="before")
    @classmethod
    def none_to_text(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("directives", "action", mode="before")
    @classmethod
    def none_to_dict(cls, value: Any) -> Any:
        return {} if value is None else value

    @model_validator(mode="after")
    def normalize(self) -> "AP2ContextResult":
        self.channel = (
            _normalize_choice(self.channel, CHANNELS, "narrative") or "narrative"
        )
        self.intent = (
            _normalize_choice(self.intent, INTENTS, "default") or "default"
        )
        if self.meta_kind not in META_KINDS:
            self.meta_kind = "instruction"
        return self


class AP2EntitiesResult(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=12)


class AP2AnalysisResult(BaseModel):
    channel: str = "narrative"
    intent: str = "default"
    context_summary: str = Field(default="", max_length=300)
    rewritten_query: str = Field(default="", max_length=800)
    narrative_query: str = Field(default="", max_length=1200)
    directives: TurnDirective = Field(default_factory=TurnDirective)
    action: ActionProposal = Field(default_factory=ActionProposal)
    meta_kind: str = "instruction"
    meta_question: str = Field(default="", max_length=800)
    entities: list[str] = Field(default_factory=list, max_length=12)


class GMPlan(BaseModel):
    intent: str = Field(default="", max_length=32)
    expected_outcome: str = Field(default="", max_length=300)
    continuity: str = Field(default="", max_length=300)
    state_change: str = Field(default="", max_length=300)


class NarrativeEffect(BaseModel):
    """GM 提出的剧情后果；具体数值由引擎校验后写入。"""

    kind: Literal[
        "hp_delta",
        "tag_add",
        "tag_remove",
        "flag_set",
        "scale_delta",
        "time_advance",
    ]
    target: str = Field(default="", max_length=120)
    field: str = Field(default="", max_length=64)
    value: Any = None
    reason: str = Field(default="", max_length=200)


class GMResult(BaseModel):
    plan: GMPlan | None = None
    effects: list[NarrativeEffect] = Field(default_factory=list)
    reply: str = Field(min_length=1)


class AttributeChangeInput(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)
    # 确定性顺序键：对话用 turn_no，世界导入用批次+窗口位置。
    source_order: int = 0


class RelationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_ref: str = Field(min_length=1, max_length=120)
    relation_label: str = Field(default="未明确", min_length=1, max_length=80)
    source_order: int = 0

    @field_validator("relation_label", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str:
        return normalize_relation_label(value)


class EntityDecision(BaseModel):
    action: Literal["create", "update", "skip"]
    matched_entity_id: str | None = None
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    canonical_name: str | None = Field(default=None, max_length=120)
    entity_type: EntityType | None = None
    aliases: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=500)
    # 本轮文本中的实际称呼，仅用于 mention 留痕；实体身份用 canonical_name
    # 或 matched_entity_id，关系引用不依赖该字段。
    surface_form: str = Field(default="", max_length=120)
    attribute_changes: list[AttributeChangeInput] = Field(
        default_factory=list, max_length=6
    )
    relations: list[RelationInput] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _validate_action(self):
        if self.action == "create":
            if not self.canonical_name or not self.entity_type:
                raise ValueError("create 决策必须提供 canonical_name 与 entity_type")
        elif self.action == "update":
            if not self.matched_entity_id:
                raise ValueError("update 决策必须提供 matched_entity_id")
        return self


class CommitResult(BaseModel):
    turn_summary: str = Field(default="", max_length=500)
    decisions: list[EntityDecision] = Field(default_factory=list, max_length=8)
