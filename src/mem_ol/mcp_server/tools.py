from __future__ import annotations

import functools
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger
from mem_ol.domain import EntityDecision, normalize_relation_label
from mem_ol.services.embedding import EmbeddingService, get_embedding_service
from mem_ol.storage.neo4j import Neo4jStore
from mem_ol.storage.postgres import PostgresStore, init_schema

logger = get_logger(__name__)


def _normalize_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    return re.sub(r"\s+", "", normalized)


def _relation_id(
    thread_id: str, source_id: str, target_id: str, relation_label: str
) -> str:
    """关系 ID 由线程、两端实体和关系标签决定；不同标签保持不同边。"""
    label = normalize_relation_label(relation_label)
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{thread_id}:rel:{source_id}:{target_id}:{label}",
        )
    )


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_dt(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _logged_tool(name: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            started = time.perf_counter()
            success = True
            error: str | None = None
            result = None
            try:
                result = func(self, *args, **kwargs)
                return result
            except Exception as exc:  # noqa: BLE001
                success = False
                error = str(exc)
                raise
            finally:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._log_tool(
                    name, kwargs, result, success, error, elapsed_ms
                )

        return wrapper

    return decorator


class EntityToolService:
    """MCP 工具的唯一实现。API 2/3 只调用这里，不直接碰数据库。"""

    def __init__(
        self,
        store: PostgresStore,
        neo4j: Neo4jStore,
        embedder: EmbeddingService,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.neo4j = neo4j
        self.embedder = embedder
        self.settings = settings or get_settings()

    def close(self) -> None:
        self.store.close()
        self.neo4j.close()

    # ---------- logging ----------

    def _log_tool(
        self,
        name: str,
        kwargs: dict[str, Any],
        result: Any,
        success: bool,
        error: str | None,
        duration_ms: int,
    ) -> None:
        try:
            arguments: dict[str, Any] = {}
            for key, value in kwargs.items():
                if isinstance(value, (list, tuple, dict)):
                    arguments[key] = f"<{type(value).__name__} len={len(value)}>"
                elif isinstance(value, str) and len(value) > 300:
                    arguments[key] = value[:300] + "..."
                else:
                    arguments[key] = value

            if isinstance(result, list):
                result_summary: dict[str, Any] = {"count": len(result)}
            elif isinstance(result, dict):
                result_summary = {
                    key: value
                    for key, value in list(result.items())[:8]
                    if not isinstance(value, (list, dict))
                }
            else:
                result_summary = {"value": str(result)[:200]}

            thread_id = kwargs.get("thread_id")
            trace_id = kwargs.get("trace_id")
            self.store.insert_tool_call_log(
                tool_name=name,
                thread_id=thread_id,
                trace_id=trace_id,
                arguments=arguments,
                result_summary=result_summary,
                success=success,
                error=error,
                duration_ms=duration_ms,
            )
        except Exception:  # noqa: BLE001
            logger.debug("工具调用日志写入失败", exc_info=True)

    # ---------- retrieval service methods (deterministic reads) ----------

    @_logged_tool("context_recall_vector")
    def context_recall_vector(
        self,
        thread_id: str,
        query: str,
        k: int = 4,
        vector: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        vector = vector or self.embedder.embed_query(query)
        rows = self.store.search_chunks(
            thread_id, vector, k=int(k), level="child"
        )
        candidates = [
            {
                "kind": "context",
                "chunk_id": str(row["id"]),
                "batch_id": str(row["batch_id"]),
                "chunk_no": row["chunk_no"],
                "parent_no": row.get("parent_no"),
                "content": row["content"],
                "parent_content": row.get("parent_content"),
                "score": float(row["score"]),
                "turn_no": row.get("turn_no"),
                "turn_start": row.get("turn_start"),
                "turn_end": row.get("turn_end"),
                "message_ids": row.get("message_ids", []),
                "route": "context",
            }
            for row in rows
        ]
        return {"candidates": candidates}

    @_logged_tool("entity_recall_keyword")
    def entity_recall_keyword(
        self, thread_id: str, keywords: list[str], limit: int = 10
    ) -> dict[str, Any]:
        rows = self.neo4j.search_entities_keyword(
            thread_id, keywords, limit=int(limit)
        )
        candidates = []
        for rank, row in enumerate(rows, start=1):
            item = dict(row)
            item["id"] = str(item.get("entity_id"))
            item["route"] = "keyword"
            item["route_rank"] = rank
            item["route_score"] = float(item.get("score", 0.0))
            candidates.append(item)
        return {"candidates": candidates}

    @_logged_tool("relation_recall_keyword")
    def relation_recall_keyword(
        self, thread_id: str, keywords: list[str], limit: int = 10
    ) -> dict[str, Any]:
        rows = self.neo4j.search_relations_keyword(
            thread_id, keywords, limit=int(limit)
        )
        return {"candidates": rows}

    @_logged_tool("entity_recall_vector")
    def entity_recall_vector(
        self,
        thread_id: str,
        query: str,
        limit: int = 10,
        vector: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        vector = vector or self.embedder.embed_query(query)
        rows = self.store.search_entity_vectors(
            thread_id, vector, limit=int(limit)
        )
        candidates = []
        for rank, row in enumerate(rows, start=1):
            item = {
                key: (str(value) if isinstance(value, uuid.UUID) else value)
                for key, value in dict(row).items()
            }
            item["id"] = str(item["entity_id"])
            item["route"] = "vector"
            item["route_rank"] = rank
            item["route_score"] = float(item.get("score", 0.0))
            candidates.append(item)
        return {"candidates": candidates}

    @_logged_tool("entity_get")
    def entity_get(
        self,
        thread_id: str,
        entity_id: str,
        include_attributes: bool = True,
        include_relations: bool = True,
    ) -> dict[str, Any]:
        result = self.neo4j.get_entity(
            thread_id,
            entity_id,
            include_attributes=include_attributes,
            include_relations=include_relations,
        )
        if result is None:
            return {"found": False}
        return {"found": True, **result}

    def characters_created_at(
        self, thread_id: str, turn_no: int
    ) -> list[dict[str, Any]]:
        return self.neo4j.list_characters_created_at(thread_id, int(turn_no))

    @_logged_tool("entity_resolve")
    def entity_resolve(
        self, thread_id: str, name: str
    ) -> dict[str, Any]:
        """按名字/别名把状态目标挂到实体；查不到返回空。"""
        result = self.neo4j.find_entity_by_name(thread_id, name)
        return {"found": result is not None, "entity": result}

    @_logged_tool("entity_neighborhood")
    def entity_neighborhood(
        self,
        thread_id: str,
        entity_ids: list[str],
        depth: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        return {
            "paths": self.neo4j.get_neighborhood(
                thread_id, entity_ids, depth=int(depth), limit=int(limit)
            )
        }

    @_logged_tool("chunk_get")
    def chunk_get(
        self, thread_id: str, chunk_nos: list[str]
    ) -> dict[str, Any]:
        rows = self.store.get_chunks_by_nos(thread_id, chunk_nos)
        chunks = []
        for row in rows:
            item = dict(row)
            item["id"] = str(item["id"])
            item["batch_id"] = str(item["batch_id"])
            item["parent_id"] = (
                str(item["parent_id"]) if item.get("parent_id") else None
            )
            chunks.append(item)
        return {"chunks": chunks}

    # ---------- write tools ----------

    @_logged_tool("retrieval_trace_write")
    def retrieval_trace_write(
        self,
        *,
        thread_id: str,
        trace_id: str,
        turn_no: int | None,
        query_text: str,
        context_summary: str | None,
        hypothetical_entities: list[dict[str, Any]],
        entity_keyword_candidates: list[dict[str, Any]],
        entity_vector_candidates: list[dict[str, Any]],
        entity_fused_candidates: list[dict[str, Any]],
        context_candidates: list[dict[str, Any]],
        rerank_scores: dict[str, Any] | None = None,
        selected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.insert_retrieval_trace_v2(
            trace_id=trace_id,
            thread_id=thread_id,
            turn_no=turn_no,
            query_text=query_text,
            context_summary=context_summary,
            hypothetical_entities=hypothetical_entities,
            entity_keyword_candidates=entity_keyword_candidates,
            entity_vector_candidates=entity_vector_candidates,
            entity_fused_candidates=entity_fused_candidates,
            context_candidates=context_candidates,
            rerank_scores=rerank_scores,
            selected=selected,
        )
        return {"trace_id": str(trace_id)}

    @_logged_tool("entity_commit_plan")
    def entity_commit_plan(
        self,
        *,
        thread_id: str,
        trace_id: str,
        turn_no: int | None,
        batch_id: str | None,
        occurred_at: str | datetime | None,
        decisions: list[dict[str, Any] | EntityDecision],
        source_chunk_nos: list[str] | None = None,
        source_message_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        tid = uuid.UUID(str(thread_id))
        if self.store.get_thread(tid) is None:
            raise ValueError(f"thread 不存在: {thread_id}")

        trid = str(trace_id)
        occurred = _parse_dt(occurred_at)
        chunk_nos = list(source_chunk_nos or [])
        message_ids = list(source_message_ids or [])

        parsed: list[EntityDecision] = [
            (
                decision
                if isinstance(decision, EntityDecision)
                else EntityDecision.model_validate(decision)
            )
            for decision in decisions
        ]

        prepared: list[dict[str, Any]] = []
        # 实体引用键：create 用 canonical_name，update 用 matched_entity_id；
        # 别名与 surface_form 只用于解析本轮内的关系端点。
        ref_to_id: dict[str, str] = {}
        seen_names: set[str] = set()
        new_ids: set[str] = set()

        def resolve_ref(value: str) -> str | None:
            if value in ref_to_id:
                return ref_to_id[value]
            normalized = _normalize_name(value)
            for key, entity_id in ref_to_id.items():
                if _normalize_name(key) == normalized:
                    return entity_id
            return None

        def resolve_target(value: str) -> str | None:
            """关系终点：本轮内的引用 → UUID → 线程内已有的实体名。"""
            resolved = resolve_ref(value)
            if resolved:
                return resolved
            try:
                return str(uuid.UUID(value))
            except ValueError:
                pass
            found = self.neo4j.find_entity_by_name(tid, value)
            return str(found["entity_id"]) if found else None

        for decision in parsed:
            if decision.action == "skip":
                prepared.append(
                    {
                        "action": "skip",
                        "entity_id": None,
                        "surface_form": decision.surface_form,
                    }
                )
                continue

            # 写图前按规范名/别名核对一次，命中已有实体就改成更新，避免重复实体。
            if decision.action == "create" and decision.canonical_name:
                existing = self.neo4j.find_entity_by_name(
                    tid, decision.canonical_name
                )
                if existing:
                    decision.action = "update"
                    decision.matched_entity_id = str(existing["entity_id"])
                    logger.info(
                        "创建前命中已有实体，改为更新",
                        extra={
                            "event": "entity.create.merged",
                            "canonical_name": decision.canonical_name,
                            "entity_id": str(existing["entity_id"]),
                        },
                    )

            if decision.action == "create":
                entity_id = str(uuid.uuid4())
                canonical_name = decision.canonical_name or ""
                if not canonical_name:
                    raise ValueError("create 决策缺少 canonical_name")
                normalized_name = _normalize_name(canonical_name)
                if normalized_name in seen_names:
                    raise ValueError(f"重复实体名: {canonical_name}")
                seen_names.add(normalized_name)
                entity_type = decision.entity_type or "other"
                aliases = _dedupe(
                    [alias.strip() for alias in decision.aliases if alias.strip()]
                )
                new_aliases = list(aliases)
                new_ids.add(entity_id)
                created_turn_no = int(turn_no or 0)
                created_batch_id = batch_id
                entity_ref = canonical_name
            else:
                if not decision.matched_entity_id:
                    raise ValueError("update 决策缺少 matched_entity_id")
                existing = self.neo4j.get_entity(
                    tid,
                    decision.matched_entity_id,
                    include_attributes=False,
                    include_relations=False,
                )
                if existing is None:
                    raise ValueError(
                        f"update 目标不存在: {decision.matched_entity_id}"
                    )
                entity_id = str(decision.matched_entity_id)
                entity = existing["entity"]
                canonical_name = entity.get("canonical_name") or ""
                if not canonical_name:
                    raise ValueError(
                        f"update 目标缺少 canonical_name: {entity_id}"
                    )
                entity_type = entity.get("entity_type") or "other"
                existing_aliases = [
                    str(alias) for alias in entity.get("aliases", [])
                ]
                new_aliases = [
                    alias
                    for alias in _dedupe(
                        [
                            alias.strip()
                            for alias in decision.aliases
                            if alias.strip()
                        ]
                    )
                    if alias not in existing_aliases
                ]
                aliases = existing_aliases + new_aliases
                created_turn_no = int(
                    entity.get("created_turn_no") or 0
                )
                created_batch_id = entity.get("created_batch_id")
                entity_ref = entity_id

            ref_to_id[entity_ref] = entity_id
            ref_to_id[entity_id] = entity_id
            for ref in [*aliases, decision.surface_form]:
                if ref:
                    ref_to_id.setdefault(ref, entity_id)
            surface_form = decision.surface_form or canonical_name

            attribute_changes = self._attribute_changes(
                decision, entity_id, tid, trid
            )
            for index, alias in enumerate(new_aliases):
                attribute_changes.append(
                    {
                        "change_id": str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"{tid}:{trid}:{entity_id}:alias:{index}:{alias}",
                            )
                        ),
                        "key": "alias",
                        "value": alias,
                        "evidence": "实体别名写入",
                    }
                )

            prepared.append(
                {
                    "action": decision.action,
                    "surface_form": surface_form,
                    "entity_id": entity_id,
                    "canonical_name": canonical_name,
                    "normalized_name": _normalize_name(canonical_name),
                    "entity_type": entity_type,
                    "aliases": aliases,
                    "new_aliases": new_aliases,
                    "summary": decision.summary,
                    "attribute_changes": attribute_changes,
                    "created_turn_no": created_turn_no,
                    "created_batch_id": created_batch_id,
                    "last_turn_no": int(turn_no or 0),
                }
            )

        relation_specs: list[dict[str, Any]] = []
        dropped_relations = 0
        for decision in parsed:
            if decision.action == "skip":
                continue
            source_ref = (
                decision.canonical_name
                if decision.action == "create"
                else decision.matched_entity_id
            )
            source_id = resolve_ref(str(source_ref)) if source_ref else None
            if not source_id:
                # 单条关系端点解析失败不能拖垮整批实体写入。
                dropped_relations += len(decision.relations)
                logger.warning(
                    "关系起点无法解析，已跳过",
                    extra={
                        "event": "entity.relation.source_unresolved",
                        "source_ref": source_ref,
                    },
                )
                continue
            for relation in decision.relations:
                target_ref = relation.target_ref.strip()
                target_id = resolve_target(target_ref)
                if not target_id:
                    dropped_relations += 1
                    logger.warning(
                        "关系目标无法解析，已跳过",
                        extra={
                            "event": "entity.relation.target_unresolved",
                            "target_ref": target_ref,
                        },
                    )
                    continue
                if source_id == target_id:
                    dropped_relations += 1
                    logger.warning(
                        "不允许自指关系，已跳过",
                        extra={
                            "event": "entity.relation.self_reference",
                            "entity_id": source_id,
                        },
                    )
                    continue
                relation_specs.append(
                    {
                        # 同一对实体、同一关系类型只有一个稳定 ID，
                        # 跨轮次/批次重复提交不会新增重复边。
                        "rel_id": _relation_id(
                            tid,
                            source_id,
                            target_id,
                            relation.relation_label,
                        ),
                        "source_id": source_id,
                        "target_id": target_id,
                        "relation_label": relation.relation_label,
                        "source_order": relation.source_order,
                    }
                )

        referenced = {
            spec["target_id"]
            for spec in relation_specs
            if spec["target_id"] not in new_ids
        } | {
            spec["source_id"]
            for spec in relation_specs
            if spec["source_id"] not in new_ids
        }
        if referenced:
            found = self.neo4j.entities_exist(tid, list(referenced))
            missing = referenced - found - new_ids
            if missing:
                raise ValueError(
                    f"关系端点不存在: {sorted(missing)}"
                )

        embed_items = [
            item for item in prepared if item["action"] != "skip"
        ]
        embed_texts = [
            f"名称：{item['canonical_name']}；"
            f"别名：{', '.join(item['aliases'])}；"
            f"类型：{item['entity_type']}"
            for item in embed_items
        ]
        if embed_texts:
            vectors = self.embedder.embed(embed_texts)
            for item, text, vector in zip(embed_items, embed_texts, vectors):
                item["embedding_text"] = text
                item["embedding"] = vector

        neo4j_result = self.neo4j.commit_plan(
            thread_id=tid,
            trace_id=trid,
            turn_no=turn_no,
            batch_id=batch_id,
            occurred_at=occurred,
            decisions=prepared,
            relation_specs=relation_specs,
            source_chunk_nos=chunk_nos,
            source_message_ids=message_ids,
        )

        index_errors: list[dict[str, str]] = []
        for item in prepared:
            if item["action"] == "skip":
                continue
            try:
                self.store.upsert_entity_index(
                    entity_id=item["entity_id"],
                    thread_id=tid,
                    canonical_name=item["canonical_name"],
                    normalized_name=item["normalized_name"],
                    entity_type=item["entity_type"],
                    aliases=item["aliases"],
                    embedding_text=item["embedding_text"],
                    embedding=item["embedding"],
                    index_status="ready",
                    created_turn_no=item["created_turn_no"],
                    created_batch_id=item["created_batch_id"],
                    last_turn_no=item["last_turn_no"],
                )
            except Exception as exc:  # noqa: BLE001
                index_errors.append(
                    {"entity_id": item["entity_id"], "error": str(exc)}
                )
                try:
                    self.store.insert_entity_index_job(
                        entity_id=item["entity_id"],
                        thread_id=tid,
                        reason="entity_index_upsert_failed",
                        error=str(exc),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("索引重试任务写入失败", exc_info=True)

            try:
                self.store.insert_entity_mention(
                    entity_id=item["entity_id"],
                    thread_id=tid,
                    batch_id=batch_id,
                    trace_id=trid,
                    turn_no=turn_no,
                    surface_form=item["surface_form"],
                    message_ids=message_ids,
                    chunk_ids=[],
                    chunk_nos=chunk_nos,
                    occurred_at=occurred,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "实体 mention 写入失败",
                    extra={
                        "event": "entity.mention.failed",
                        "entity_id": item["entity_id"],
                        "error": str(exc),
                    },
                )

        return {
            "trace_id": trid,
            "decisions": neo4j_result["decisions"],
            "relation_count": neo4j_result["relation_count"],
            "index_errors": index_errors,
        }

    @staticmethod
    def _attribute_changes(
        decision: EntityDecision,
        entity_id: str,
        thread_id: uuid.UUID,
        trace_id: str,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for index, change in enumerate(decision.attribute_changes):
            changes.append(
                {
                    "change_id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{thread_id}:{trace_id}:{entity_id}:attr:"
                            f"{index}:{change.key}:{change.value}",
                        )
                    ),
                    "key": change.key,
                    "value": change.value,
                    "evidence": change.evidence,
                    "source_order": change.source_order,
                }
            )
        return changes


_tool_service: EntityToolService | None = None


def get_tool_service() -> EntityToolService:
    global _tool_service
    if _tool_service is None:
        settings = get_settings()
        schema_path = (
            Path(__file__).resolve().parents[1] / "storage" / "schema.sql"
        )
        init_schema(settings.postgres_dsn, schema_path)
        store = PostgresStore(settings.postgres_dsn)
        neo4j = Neo4jStore(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
        )
        neo4j.init_schema()
        _tool_service = EntityToolService(
            store=store,
            neo4j=neo4j,
            embedder=get_embedding_service(),
            settings=settings,
        )
    return _tool_service


def close_tool_service() -> None:
    global _tool_service
    if _tool_service is not None:
        _tool_service.close()
        _tool_service = None
