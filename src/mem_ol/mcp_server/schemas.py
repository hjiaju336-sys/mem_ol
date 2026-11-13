from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from mem_ol.log import get_logger

logger = get_logger(__name__)

EntityType = Literal[
    "character",
    "location",
    "item",
    "faction",
    "event",
    "concept",
    "other",
]

RELATION_TYPES = {
    "located_in",
    "owns",
    "member_of",
    "knows",
    "ally",
    "enemy",
    "causes",
    "part_of",
    "related_to",
}

RelationType = Literal[
    "located_in",
    "owns",
    "member_of",
    "knows",
    "ally",
    "enemy",
    "causes",
    "part_of",
    "related_to",
]


def normalize_relation_type(value: Any) -> str:
    """模型偶发输出枚举外关系词时降级，避免整轮提交被丢弃。"""
    text = str(value or "").strip()
    if text in RELATION_TYPES:
        return text
    logger.warning(
        "未知关系类型，降级为 related_to",
        extra={"event": "relation.type.fallback", "value": text},
    )
    return "related_to"


class AP2ContextResult(BaseModel):
    context_summary: str = Field(default="", max_length=300)
    rewritten_query: str = Field(default="", max_length=800)


class AP2EntitiesResult(BaseModel):
    entities: list[str] = Field(default_factory=list, max_length=12)


class AP2AnalysisResult(BaseModel):
    context_summary: str = Field(default="", max_length=300)
    rewritten_query: str = Field(default="", max_length=800)
    entities: list[str] = Field(default_factory=list, max_length=12)


class AttributeChangeInput(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)
    # 确定性顺序键：对话用 turn_no，世界导入用批次+窗口位置。
    source_order: int = 0


class RelationInput(BaseModel):
    target_ref: str = Field(min_length=1, max_length=120)
    relation_type: RelationType
    evidence: str = Field(default="", max_length=2000)
    source_order: int = 0

    @field_validator("relation_type", mode="before")
    @classmethod
    def _fallback_relation_type(cls, value: Any) -> str:
        return normalize_relation_type(value)


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
