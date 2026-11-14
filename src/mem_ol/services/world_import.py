from __future__ import annotations

import json
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.domain import (
    AttributeChangeInput,
    EntityDecision,
    EntityType,
    RelationInput,
    normalize_relation_label,
)
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.pipelines.memory import MemoryPipeline
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.services.prompts import load_prompt, render_prompt
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class WorldAttribute(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=500)
    source_order: int = 0


class WorldEntity(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    type: EntityType
    description: str = Field(default="", max_length=500)
    attributes: list[WorldAttribute] = Field(default_factory=list, max_length=4)


class WorldRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ref: str = Field(min_length=1, max_length=120)
    target_ref: str = Field(min_length=1, max_length=120)
    relation_label: str = Field(default="未明确", min_length=1, max_length=80)
    source_order: int = 0

    @field_validator("relation_label", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str:
        return normalize_relation_label(value)


class WorldImportExtraction(BaseModel):
    entities: list[WorldEntity] = Field(default_factory=list)
    relations: list[WorldRelation] = Field(default_factory=list)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", "", value)


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _dedupe_attributes(
    values: Sequence[WorldAttribute],
) -> list[WorldAttribute]:
    seen: dict[tuple[str, str], WorldAttribute] = {}
    result: list[WorldAttribute] = []
    for attribute in values:
        key = (attribute.key.strip(), attribute.value.strip())
        if key not in seen:
            seen[key] = attribute
            result.append(attribute)
        else:
            seen[key].source_order = max(
                seen[key].source_order, attribute.source_order
            )
    return result


_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;…])")


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """把超长段落按句末标点切开；无标点的长串按字符硬切。"""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_BOUNDARY.split(paragraph):
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(
                sentence[start : start + max_chars]
                for start in range(0, len(sentence), max_chars)
            )
            continue
        if current and len(current) + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return pieces


def _split_text_windows(
    text: str, max_chars: int = 20000
) -> list[str]:
    """切文档级窗口，保证每窗不超过 max_chars；实体抽取不复用检索父子段。"""
    text = text.strip()
    if not text:
        return []
    # 先按空行切；没有空行时按单换行切，避免整篇变成一个窗口。
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n|\n", text)
        if paragraph.strip()
    ]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
        else:
            units.extend(_split_long_paragraph(paragraph, max_chars))

    windows: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 2 > max_chars:
            windows.append(current)
            current = unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        windows.append(current)
    return windows


def _apply_window_order(
    extraction: WorldImportExtraction, window_index: int
) -> None:
    """为窗口内条目写入相对顺序，后续再叠加批次偏移。"""
    base = window_index * 1_000
    for entity in extraction.entities:
        for index, attribute in enumerate(entity.attributes):
            attribute.source_order = base + index
    for index, relation in enumerate(extraction.relations):
        relation.source_order = base + index


def _attach_source_orders(
    consolidated: WorldImportExtraction,
    extractions: Sequence[WorldImportExtraction],
) -> None:
    """合并结果不带顺序时，按原文匹配回填，不依赖 LLM 保留元数据。"""
    alias_to_canonical: dict[str, str] = {}
    attribute_orders: dict[tuple[str, str, str], int] = {}
    relation_orders: dict[tuple[str, str, str], int] = {}
    entity_orders: dict[str, int] = {}

    for extraction in extractions:
        for entity in extraction.entities:
            canonical = _normalize(entity.canonical_name)
            names = [entity.canonical_name, *entity.aliases]
            for name in names:
                alias_to_canonical[_normalize(name)] = canonical
            first = min(
                (
                    attribute.source_order
                    for attribute in entity.attributes
                    if attribute.source_order
                ),
                default=0,
            )
            entity_orders[canonical] = max(
                entity_orders.get(canonical, 0), first
            )
            for attribute in entity.attributes:
                for name in names:
                    key = (
                        _normalize(name),
                        _normalize(attribute.key),
                        _normalize(attribute.value),
                    )
                    attribute_orders[key] = max(
                        attribute_orders.get(key, 0),
                        attribute.source_order,
                    )

    for extraction in extractions:
        for relation in extraction.relations:
            source = alias_to_canonical.get(
                _normalize(relation.source_ref),
                _normalize(relation.source_ref),
            )
            target = alias_to_canonical.get(
                _normalize(relation.target_ref),
                _normalize(relation.target_ref),
            )
            key = (source, target, relation.relation_label)
            relation_orders[key] = max(
                relation_orders.get(key, 0), relation.source_order
            )

    consolidated_alias: dict[str, str] = {}
    for entity in consolidated.entities:
        canonical = _normalize(entity.canonical_name)
        names = [entity.canonical_name, *entity.aliases]
        for name in names:
            consolidated_alias[_normalize(name)] = canonical
        for attribute in entity.attributes:
            order = attribute.source_order
            for name in names:
                key = (
                    _normalize(name),
                    _normalize(attribute.key),
                    _normalize(attribute.value),
                )
                order = max(order, attribute_orders.get(key, 0))
            attribute.source_order = order or entity_orders.get(canonical, 0)

    for relation in consolidated.relations:
        source = consolidated_alias.get(
            _normalize(relation.source_ref),
            _normalize(relation.source_ref),
        )
        target = consolidated_alias.get(
            _normalize(relation.target_ref),
            _normalize(relation.target_ref),
        )
        key = (source, target, relation.relation_label)
        relation.source_order = max(
            relation.source_order, relation_orders.get(key, 0)
        )


def _group_extractions(
    extractions: Sequence[WorldImportExtraction],
    max_chars: int,
) -> list[list[WorldImportExtraction]]:
    """按文档顺序分组，保证单次合并输入不超过 max_chars。"""
    groups: list[list[WorldImportExtraction]] = []
    current: list[WorldImportExtraction] = []
    size = 0
    for item in extractions:
        item_size = len(json.dumps(item.model_dump(), ensure_ascii=False))
        if current and size + item_size > max_chars:
            groups.append(current)
            current = []
            size = 0
        current.append(item)
        size += item_size
    if current:
        groups.append(current)
    return groups


def _merge_extractions(
    extractions: Sequence[WorldImportExtraction],
) -> tuple[dict[str, WorldEntity], list[tuple[str, str, str, int]]]:
    """按 canonical_name 做简单合并；实体去重/别名归一由提示词约束。"""
    merged: dict[str, WorldEntity] = {}
    alias_to_key: dict[str, str] = {}

    for extraction in extractions:
        for entity in extraction.entities:
            key = _normalize(entity.canonical_name)
            if not key:
                continue
            if key not in merged:
                merged[key] = entity.model_copy(deep=True)
            else:
                current = merged[key]
                current.aliases = _dedupe(
                    [*current.aliases, *entity.aliases]
                )[:8]
                current.attributes = _dedupe_attributes(
                    [*current.attributes, *entity.attributes]
                )[:4]
                if len(entity.description) > len(current.description):
                    current.description = entity.description

            for alias in [entity.canonical_name, *entity.aliases]:
                alias_key = _normalize(alias)
                if alias_key:
                    alias_to_key.setdefault(alias_key, key)

    relations: list[tuple[str, str, str, int]] = []
    relation_orders: dict[tuple[str, str, str], int] = {}
    for extraction in extractions:
        for relation in extraction.relations:
            source_key = alias_to_key.get(_normalize(relation.source_ref))
            target_key = alias_to_key.get(_normalize(relation.target_ref))
            if not source_key or not target_key or source_key == target_key:
                continue
            relation_key = (
                source_key,
                target_key,
                relation.relation_label,
            )
            relation_orders[relation_key] = max(
                relation_orders.get(relation_key, 0),
                relation.source_order,
            )

    for (source_key, target_key, relation_label), order in (
        relation_orders.items()
    ):
        relations.append(
            (source_key, target_key, relation_label, order)
        )

    return merged, relations


def _fallback_consolidate(
    extractions: Sequence[WorldImportExtraction],
) -> WorldImportExtraction:
    """检查 agent 失败时的简单兜底：只按 exact canonical 合并。"""
    entities, relations = _merge_extractions(extractions)
    relation_models: list[WorldRelation] = []
    for source_key, target_key, relation_label, order in relations:
        source = entities.get(source_key)
        target = entities.get(target_key)
        if source is None or target is None:
            continue
        relation_models.append(
            WorldRelation(
                source_ref=source.canonical_name,
                target_ref=target.canonical_name,
                relation_label=relation_label,
                source_order=order,
            )
        )
    return WorldImportExtraction(
        entities=list(entities.values()),
        relations=relation_models,
    )


def _existing_name_map(
    entities: Sequence[dict[str, Any]],
) -> dict[str, str]:
    """已有实体的规范名/别名 → entity_id，用于增量导入匹配。"""
    mapping: dict[str, str] = {}
    for entity in entities:
        names = [
            entity.get("canonical_name"),
            *(entity.get("aliases") or []),
        ]
        for name in names:
            key = _normalize(str(name or ""))
            if key:
                mapping.setdefault(key, str(entity["entity_id"]))
    return mapping


def _build_decisions(
    extraction: WorldImportExtraction,
    batch_seq: int = 1,
    existing_by_name: dict[str, str] | None = None,
) -> list[EntityDecision]:
    entities_by_key: dict[str, WorldEntity] = {}
    alias_to_key: dict[str, str] = {}
    for entity in extraction.entities:
        key = _normalize(entity.canonical_name)
        if not key or key in entities_by_key:
            continue
        entities_by_key[key] = entity
        for alias in [entity.canonical_name, *entity.aliases]:
            alias_key = _normalize(alias)
            if alias_key:
                alias_to_key.setdefault(alias_key, key)

    matched_ids: dict[str, str | None] = {}
    entity_refs: dict[str, str] = {}
    for key, entity in entities_by_key.items():
        matched_id = None
        if existing_by_name:
            for name in [entity.canonical_name, *entity.aliases]:
                matched_id = existing_by_name.get(_normalize(name))
                if matched_id:
                    break
        matched_ids[key] = matched_id
        entity_refs[key] = matched_id or entity.canonical_name

    relation_orders: dict[tuple[str, str, str], int] = {}
    relations_by_source: dict[str, list[tuple[str, str, int]]] = {}
    for relation in extraction.relations:
        source_key = alias_to_key.get(_normalize(relation.source_ref))
        target_key = alias_to_key.get(_normalize(relation.target_ref))
        if not source_key or not target_key or source_key == target_key:
            continue
        relation_key = (source_key, target_key, relation.relation_label)
        relation_orders[relation_key] = max(
            relation_orders.get(relation_key, 0),
            relation.source_order,
        )

    for (source_key, target_key, relation_label), order in (
        relation_orders.items()
    ):
        relations_by_source.setdefault(source_key, []).append(
            (target_key, relation_label, order)
        )

    batch_offset = int(batch_seq) * 1_000_000
    decisions: list[EntityDecision] = []
    for source_key, entity in entities_by_key.items():
        relation_inputs: list[RelationInput] = []
        for target_key, relation_label, order in relations_by_source.get(
            source_key, []
        )[:6]:
            relation_inputs.append(
                RelationInput(
                    target_ref=entity_refs[target_key],
                    relation_label=relation_label,
                    source_order=batch_offset + order,
                )
            )

        attribute_inputs = [
            AttributeChangeInput(
                key=attribute.key,
                value=attribute.value,
                source_order=batch_offset + attribute.source_order,
            )
            for attribute in entity.attributes[:6]
            if attribute.key and attribute.value
        ]
        aliases = [
            alias
            for alias in entity.aliases
            if alias and alias != entity.canonical_name
        ][:8]

        matched_id = matched_ids[source_key]
        decisions.append(
            EntityDecision(
                action="update" if matched_id else "create",
                matched_entity_id=matched_id,
                match_confidence=1.0 if matched_id else 0.0,
                canonical_name=entity.canonical_name,
                entity_type=entity.type,
                aliases=aliases,
                summary=entity.description,
                attribute_changes=attribute_inputs,
                relations=relation_inputs,
            )
        )
    return decisions


class WorldImportService:
    """世界导入：先向量化上下文，再从作品文本初始化实体与关系。"""

    def __init__(
        self,
        tools: EntityToolService,
        store: PostgresStore | None = None,
        settings: Settings | None = None,
        llm: LLMService | None = None,
        memory: MemoryPipeline | None = None,
    ) -> None:
        self.tools = tools
        self.store = store or tools.store
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_service()
        self.memory = memory or MemoryPipeline(
            self.store, settings=self.settings
        )
        self._consolidation_failures = 0
        self._consolidation_levels = 0

    def import_world(
        self,
        thread_id: str,
        text: str,
        source_name: str | None = None,
        *,
        append: bool = False,
        doc_id: str | None = None,
        char_start: int = 0,
        char_end: int | None = None,
    ) -> dict[str, Any]:
        tid = str(thread_id)
        text = text.strip()
        if not text:
            raise ValueError("世界文本为空")
        if self.store.get_thread(tid) is None:
            raise ValueError(f"thread 不存在: {tid}")

        existing_entities = self.store.list_entity_index(tid)
        if existing_entities and not append:
            raise ValueError(
                "该会话已有实体；补充导入请使用 append=True"
            )
        doc_key = doc_id or source_name or "default"
        start = int(char_start or 0)
        end = int(char_end if char_end is not None else start + len(text))
        if end <= start:
            raise ValueError("char_end 必须大于 char_start")
        for batch in self.store.list_batches(tid):
            if batch["kind"] != "worldview" or batch["doc_id"] != doc_key:
                continue
            if batch["char_start"] is None or batch["char_end"] is None:
                continue
            if batch["status"] in ("pending", "processing"):
                raise ValueError("该文档区间仍在导入中")
            if start < batch["char_end"] and end > batch["char_start"]:
                raise ValueError(
                    f"导入区间重叠: [{start}, {end}) 与已有批次 "
                    f"[{batch['char_start']}, {batch['char_end']})"
                )
        existing_by_name = (
            _existing_name_map(existing_entities) if append else {}
        )
        self._consolidation_failures = 0
        self._consolidation_levels = 0

        with log_context(thread_id=tid):
            # 短文本直接抽；长文本按固定字数切窗，单次输入输出都可控。
            window_chars = self.settings.world_extract_window_chars
            windows = (
                [text]
                if len(text) <= window_chars
                else _split_text_windows(text, max_chars=window_chars)
            )
            # 上下文向量化与实体抽取互不依赖，并行执行。
            with ThreadPoolExecutor(max_workers=2) as executor:
                batch_future = executor.submit(
                    self.memory.ingest_worldview,
                    tid,
                    text,
                    source_name,
                    doc_id=doc_key,
                    char_start=start,
                    char_end=end,
                )
                extraction_future = executor.submit(
                    self._extract_windows, windows, source_name
                )
                extractions, failed_windows = extraction_future.result()
                # 单窗没有跨窗重复，直接使用抽取结果，省一次 LLM 调用。
                consolidated = (
                    extractions[0]
                    if len(extractions) == 1
                    else self._consolidate(extractions, source_name)
                )
                _attach_source_orders(consolidated, extractions)
                # 向量化仍在后台跑，等实体决策完成后再取批次结果。
                batch = batch_future.result()
                decisions = _build_decisions(
                    consolidated,
                    int(batch["batch_seq"]),
                    existing_by_name,
                )

            chunks = self.store.list_chunks(tid, batch["id"])
            parents = [chunk for chunk in chunks if chunk["level"] == "parent"]
            # 实体溯源终点是图里的实体；块号只服务向量召回，不挂到实体上。
            source_chunk_nos: list[str] = []
            trace_id = str(uuid.uuid4())
            commit_result: dict[str, Any] = {
                "decisions": [],
                "relation_count": 0,
                "index_errors": [],
            }
            if decisions:
                commit_result = self.tools.entity_commit_plan(
                    thread_id=tid,
                    trace_id=trace_id,
                    turn_no=None,
                    batch_id=batch["id"],
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    decisions=[
                        decision.model_dump() for decision in decisions
                    ],
                    source_chunk_nos=source_chunk_nos,
                    source_message_ids=[],
                )

            summary = {
                "thread_id": tid,
                "batch_id": str(batch["id"]),
                "batch_seq": batch["batch_seq"],
                "doc_id": doc_key,
                "char_start": start,
                "char_end": end,
                "appended": bool(existing_entities),
                "char_count": len(text),
                "chunk_count": batch["chunk_count"],
                "parent_chunks": len(parents),
                "extraction_windows": len(windows),
                "failed_windows": len(failed_windows),
                "consolidation_levels": self._consolidation_levels,
                "consolidation_failures": self._consolidation_failures,
                "degraded": bool(
                    self._consolidation_failures or failed_windows
                ),
                "entity_count": len(decisions),
                "updated_entities": sum(
                    1 for decision in decisions if decision.action == "update"
                ),
                "relation_count": commit_result.get("relation_count", 0),
                "index_errors": commit_result.get("index_errors", []),
                "trace_id": trace_id,
            }
            logger.info(
                "世界导入完成",
                extra={
                    "event": "world.import.done",
                    **{
                        key: summary[key]
                        for key in (
                            "batch_seq",
                            "chunk_count",
                            "failed_windows",
                            "consolidation_failures",
                            "degraded",
                            "entity_count",
                            "updated_entities",
                            "relation_count",
                        )
                    },
                },
            )
            return summary

    def _consolidate(
        self,
        extractions: Sequence[WorldImportExtraction],
        source_name: str | None,
    ) -> WorldImportExtraction:
        """按输入大小分层合并，避免一次塞入全部窗口。"""
        items = list(extractions)
        if not items:
            return WorldImportExtraction()
        level = 1
        while len(items) > 1:
            groups = _group_extractions(
                items, self.settings.world_consolidate_max_chars
            )
            if all(len(group) == 1 for group in groups):
                groups = [
                    items[index : index + 2]
                    for index in range(0, len(items), 2)
                ]
            logger.info(
                "世界实体分层合并",
                extra={
                    "event": "world.consolidate.level",
                    "level": level,
                    "input_count": len(items),
                    "group_count": len(groups),
                },
            )
            merged: list[WorldImportExtraction | None] = [None] * len(groups)
            with ThreadPoolExecutor(
                max_workers=min(3, len(groups))
            ) as executor:
                futures = {
                    executor.submit(
                        self._consolidate_once, group, source_name
                    ): index
                    for index, group in enumerate(groups)
                    if len(group) > 1
                }
                for index, group in enumerate(groups):
                    if len(group) == 1:
                        merged[index] = group[0]
                for future, index in futures.items():
                    merged[index] = future.result()
            items = [item for item in merged if item is not None]
            level += 1
            self._consolidation_levels = level - 1
            if level > 8:
                break
        return items[0]

    def _consolidate_once(
        self,
        extractions: Sequence[WorldImportExtraction],
        source_name: str | None,
    ) -> WorldImportExtraction:
        """检查修复 agent：合并一批抽取结果中的重复实体、别名和关系。"""
        try:
            system, user_template = load_prompt("world_consolidate.md")
            user_prompt = render_prompt(
                user_template,
                source_name=source_name or "未命名作品",
                extractions_json=json.dumps(
                    [item.model_dump() for item in extractions],
                    ensure_ascii=False,
                ),
            )
            result = self.llm.chat_json_model(
                WorldImportExtraction,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=None,
                timeout=self.settings.world_extract_timeout,
            )
            if not result.entities and any(
                item.entities for item in extractions
            ):
                raise ValueError("检查 agent 返回空实体")
            return result
        except Exception as exc:  # noqa: BLE001
            self._consolidation_failures += 1
            logger.exception(
                "世界实体检查修复失败，使用 exact-canonical 兜底",
                extra={
                    "event": "world.consolidate.failed",
                    "error": str(exc),
                },
            )
            return _fallback_consolidate(extractions)

    def _extract_windows(
        self,
        windows: Sequence[str],
        source_name: str | None,
    ) -> tuple[list[WorldImportExtraction], list[dict[str, Any]]]:
        """并行抽取各窗口；结果按窗口原始顺序返回，失败窗口单独上报。"""
        if not windows:
            return [], []
        results: list[WorldImportExtraction | None] = [None] * len(windows)
        failed: list[dict[str, Any]] = []
        workers = max(
            1,
            min(self.settings.world_extract_workers, len(windows)),
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._extract_window_with_retry, window_text, source_name
                ): index
                for index, window_text in enumerate(windows)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    extraction = future.result()
                    _apply_window_order(extraction, index)
                    results[index] = extraction
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {
                            "index": index,
                            "chars": len(windows[index]),
                            "error": str(exc),
                        }
                    )
                    logger.exception(
                        "世界实体抽取失败",
                        extra={
                            "event": "world.extract.failed",
                            "window_index": index,
                            "chars": len(windows[index]),
                            "error": str(exc),
                        },
                    )
        return [item for item in results if item is not None], failed

    def _extract_window_with_retry(
        self, window_text: str, source_name: str | None
    ) -> WorldImportExtraction:
        """单窗失败时二分重试；限流退避由 LLMService 统一处理。"""
        try:
            return self._extract_window(window_text, source_name)
        except Exception as exc:  # noqa: BLE001
            if len(window_text) <= 600:
                raise
            split_at = len(window_text) // 2
            for cursor in range(split_at, max(split_at - 200, 0), -1):
                if window_text[cursor] in "。！？；\n":
                    split_at = cursor + 1
                    break
            logger.warning(
                "世界实体抽取失败，二分窗口重试",
                extra={
                    "event": "world.extract.split",
                    "error": str(exc),
                    "chars": len(window_text),
                },
            )
            left = self._extract_window_with_retry(
                window_text[:split_at], source_name
            )
            right = self._extract_window_with_retry(
                window_text[split_at:], source_name
            )
            return WorldImportExtraction(
                entities=left.entities + right.entities,
                relations=left.relations + right.relations,
            )

    def _extract_window(
        self, world_text: str, source_name: str | None
    ) -> WorldImportExtraction:
        system, user_template = load_prompt("world_import.md")
        user_prompt = render_prompt(
            user_template,
            source_name=source_name or "未命名作品",
            world_text=world_text,
        )
        return self.llm.chat_json_model(
            WorldImportExtraction,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=None,
            timeout=self.settings.world_extract_timeout,
        )
