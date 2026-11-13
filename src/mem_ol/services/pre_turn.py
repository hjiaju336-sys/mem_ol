from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.mcp_server.schemas import (
    AP2AnalysisResult,
    AP2ContextResult,
    AP2EntitiesResult,
)
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.services.prompts import load_prompt, render_prompt
from mem_ol.services.query_enhancer import QueryEnhancer
from mem_ol.services.rerank import RerankService, get_rerank_service
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class PreTurnService:
    """API 2：假设实体 → 关键词/向量召回 → 上下文召回 → recall packet。"""

    def __init__(
        self,
        tools: EntityToolService,
        store: PostgresStore | None = None,
        llm: LLMService | None = None,
        settings: Settings | None = None,
        rerank: RerankService | None = None,
        query_enhancer: QueryEnhancer | None = None,
    ) -> None:
        self.tools = tools
        self.store = store or tools.store
        self.llm = llm or get_llm_service()
        self.settings = settings or get_settings()
        self.rerank = rerank or get_rerank_service()
        self.query_enhancer = query_enhancer or QueryEnhancer()

    def run(
        self,
        thread_id: uuid.UUID | str,
        user_message: str,
        *,
        turn_no: int | None = None,
    ) -> dict[str, Any]:
        tid = str(thread_id)
        trace_id = str(uuid.uuid4())

        with log_context(thread_id=tid):
            # 确定性读取：直接由编排层取最近 N 轮（含已入库）并拼 prompt，不走 MCP。
            recent = self.store.get_recent_turns(
                tid, self.settings.dialogue_history_turns
            )
            recent_turn_numbers = {
                int(message["turn_no"]) for message in recent
            }
            previous = self._previous_turn(recent, turn_no)
            analysis = self._analyze(
                tid, user_message, recent, previous
            )
            if previous and analysis.context_summary:
                try:
                    self.store.upsert_turn_summary(
                        tid,
                        int(previous["turn_no"]),
                        analysis.context_summary,
                        previous["message_ids"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "单轮摘要写入失败",
                        extra={
                            "event": "plot.turn_summary.failed",
                            "error": str(exc),
                        },
                    )
            effective_query = analysis.rewritten_query or user_message
            terms = _dedupe(analysis.entities)

            # 实体检索和上下文检索的 query 一次编码，线程里只做数据库召回。
            entity_query = " ".join(terms)
            context_query = effective_query
            if analysis.context_summary:
                context_query = (
                    f"{effective_query}\n{analysis.context_summary}"
                )
            queries = ([entity_query] if terms else []) + [context_query]
            encoded: list[list[float]] = []
            try:
                encoded = self.tools.embedder.embed_queries(queries)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "查询向量批量编码失败",
                    extra={
                        "event": "api2.query.embed.failed",
                        "error": str(exc),
                    },
                )
            encoded_ok = len(encoded) == len(queries)
            entity_vector = encoded[0] if terms and encoded_ok else None
            context_vector = encoded[-1] if encoded_ok else None

            keyword_candidates: list[dict[str, Any]] = []
            vector_candidates: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=4) as executor:
                context_future = executor.submit(
                    self._context_recall, tid, context_query, context_vector
                )
                plot_future = executor.submit(
                    self._plot_recall, tid, context_vector, turn_no
                )
                keyword_future = None
                vector_future = None
                if terms:
                    keyword_future = executor.submit(
                        self.tools.entity_recall_keyword,
                        thread_id=tid,
                        keywords=terms,
                        limit=self.settings.entity_recall_top_k,
                    )
                    vector_future = executor.submit(
                        self.tools.entity_recall_vector,
                        thread_id=tid,
                        query=entity_query,
                        limit=self.settings.entity_recall_top_k,
                        vector=entity_vector,
                    )
                if keyword_future is not None:
                    try:
                        keyword_candidates = keyword_future.result().get(
                            "candidates", []
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "关键词实体召回失败",
                            extra={
                                "event": "api2.entity.keyword.failed",
                                "error": str(exc),
                            },
                        )
                if vector_future is not None:
                    try:
                        vector_candidates = vector_future.result().get(
                            "candidates", []
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "向量实体召回失败",
                            extra={
                                "event": "api2.entity.vector.failed",
                                "error": str(exc),
                            },
                        )
                context_candidates = context_future.result()
                plot_context = plot_future.result()

            fused = _fuse_entities(keyword_candidates, vector_candidates)
            entity_candidates = self._load_details(
                tid, fused[: self.settings.entity_candidate_top_k]
            )
            entity_candidates, graph_paths = self._expand_graph(
                tid, entity_candidates
            )

            # 最近 N 轮已经作为原始上下文常驻，召回结果里完全落在该窗口内的片段去掉。
            context_candidates = _exclude_recent_context(
                context_candidates, recent_turn_numbers
            )

            # 同一父块的多个子块只保留得分最高的一个；其余记录在 child_chunk_nos。
            context_candidates = _dedupe_contexts_by_parent(context_candidates)
            context_candidates = context_candidates[
                : self.settings.context_candidate_top_k
            ]

            reranked = self.rerank.rerank(
                query=effective_query,
                entities=entity_candidates,
                contexts=context_candidates,
                entity_top_k=self.settings.entity_candidate_top_k,
                context_top_k=self.settings.context_candidate_top_k,
            )
            entity_candidates = reranked["entities"]
            context_candidates = reranked["contexts"]
            rerank_scores = reranked["scores"]

            selected_ids = {
                str(item["entity_id"]) for item in entity_candidates
            }
            graph_paths = [
                path
                for path in graph_paths
                if {
                    str(node.get("entity_id"))
                    for node in path.get("nodes", [])
                }
                & selected_ids
            ][:6]
            selected = {
                "entity_ids": [item["entity_id"] for item in entity_candidates],
                "context_parents": [
                    item.get("parent_no") or item.get("chunk_no")
                    for item in context_candidates
                ],
                "child_chunk_nos": [
                    item.get("chunk_no") for item in context_candidates
                ],
                "graph_paths": graph_paths,
                "plot_context": plot_context,
                "query_analysis": {
                    "rewritten_query": effective_query,
                    "entities": terms,
                },
            }
            try:
                self.tools.retrieval_trace_write(
                    thread_id=tid,
                    trace_id=trace_id,
                    turn_no=turn_no,
                    query_text=user_message,
                    context_summary=analysis.context_summary,
                    hypothetical_entities=[
                        {"canonical_name": term} for term in terms
                    ],
                    entity_keyword_candidates=keyword_candidates,
                    entity_vector_candidates=vector_candidates,
                    entity_fused_candidates=fused,
                    context_candidates=context_candidates,
                    rerank_scores=rerank_scores,
                    selected=selected,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "召回轨迹写入失败",
                    extra={"event": "api2.trace.failed", "error": str(exc)},
                )

            logger.info(
                "API 2 完成",
                extra={
                    "event": "api2.pre_turn.done",
                    "turn_no": turn_no,
                    "term_count": len(terms),
                    "entity_candidate_count": len(entity_candidates),
                    "context_candidate_count": len(context_candidates),
                    "graph_path_count": len(graph_paths),
                    "plot_arc_count": len(plot_context.get("arcs", [])),
                    "plot_turn_count": len(plot_context.get("turns", [])),
                },
            )

            return {
                "trace_id": trace_id,
                "turn_no": turn_no,
                "context_summary": analysis.context_summary,
                "entities": terms,
                "hypothetical_entities": [
                    {"canonical_name": term} for term in terms
                ],
                "entity_keyword_candidates": keyword_candidates,
                "entity_vector_candidates": vector_candidates,
                "entity_candidates": entity_candidates,
                "context_candidates": context_candidates,
                "graph_paths": graph_paths,
                "plot_context": plot_context,
                "query_analysis": {
                    "rewritten_query": effective_query,
                    "entities": terms,
                },
            }

    def _expand_graph(
        self, thread_id: str, seeds: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把种子实体扩展到 1~2 跳邻居；节点/边/环路去重。"""
        if not seeds:
            return seeds, []
        seed_ids = [
            str(item["entity_id"])
            for item in seeds
            if item.get("entity_id")
        ]
        if not seed_ids:
            return seeds, []

        try:
            result = self.tools.entity_neighborhood(
                thread_id=thread_id,
                entity_ids=seed_ids,
                depth=self.settings.graph_depth,
                limit=self.settings.graph_path_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "实体多跳扩展失败",
                extra={"event": "api2.graph.failed", "error": str(exc)},
            )
            return seeds, []

        graph_paths: list[dict[str, Any]] = []
        seen_paths: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
        neighbor_distance: dict[str, int] = {}
        neighbor_via: dict[str, str] = {}
        seed_set = set(seed_ids)

        for path in result.get("paths", []):
            nodes = path.get("nodes", [])
            edges = path.get("edges", [])
            ids = [str(node.get("entity_id")) for node in nodes]
            # 跳过环路：同一路径里重复出现实体。
            if not ids or len(ids) != len(set(ids)):
                continue
            key = (
                tuple(ids),
                tuple(str(edge.get("rel_id")) for edge in edges),
            )
            if key in seen_paths:
                continue
            seen_paths.add(key)
            graph_paths.append({"nodes": nodes, "edges": edges})

            for distance, node in enumerate(nodes):
                entity_id = str(node.get("entity_id"))
                if entity_id in seed_set:
                    continue
                if distance < neighbor_distance.get(entity_id, 999):
                    neighbor_distance[entity_id] = distance
                    neighbor_via[entity_id] = ids[0]

        graph_paths = graph_paths[: self.settings.graph_path_limit]
        new_ids = [
            entity_id
            for entity_id, _ in sorted(
                neighbor_distance.items(), key=lambda item: item[1]
            )
            if entity_id not in seed_set
        ][: self.settings.graph_neighbor_top_k]
        if not new_ids:
            return seeds, graph_paths

        details = self._load_details(
            thread_id, [{"entity_id": entity_id} for entity_id in new_ids]
        )
        by_id = {str(item["entity_id"]): item for item in details}
        for entity_id in new_ids:
            item = by_id.get(entity_id)
            if item is None:
                continue
            item["routes"] = ["graph"]
            item["graph_distance"] = neighbor_distance[entity_id]
            item["graph_via"] = neighbor_via.get(entity_id)
            item["rrf_score"] = 1.0 / (1.0 + neighbor_distance[entity_id])
            seeds.append(item)

        return seeds, graph_paths

    def _plot_recall(
        self,
        thread_id: str,
        query_vector: Sequence[float] | None,
        turn_no: int | None,
    ) -> dict[str, Any]:
        """主线：全局梗概 + 章节摘要；无章节时回退到已归档单轮摘要。"""
        try:
            global_node = self.store.get_global_plot(thread_id)
            cutoff = (
                max(0, int(turn_no) - self.settings.dialogue_history_turns)
                if turn_no
                else None
            )
            limit = max(1, int(self.settings.plot_recall_top_k))
            min_score = float(self.settings.plot_recall_min_score)
            arcs: list[dict[str, Any]] = []
            if cutoff is not None:
                arcs = self.store.list_arcs(thread_id, cutoff, limit=limit)
                if query_vector:
                    arcs.extend(
                        [
                            arc
                            for arc in self.store.search_arc_summaries(
                                thread_id,
                                query_vector,
                                max_turn_end=cutoff,
                                limit=limit,
                            )
                            if float(arc.get("score", 0.0)) >= min_score
                        ]
                    )
            by_id: dict[str, dict[str, Any]] = {}
            for arc in arcs:
                by_id[str(arc.get("id"))] = {
                    "turn_start": arc.get("turn_start"),
                    "turn_end": arc.get("turn_end"),
                    "summary": arc.get("summary", ""),
                }
            ordered_arcs = sorted(
                by_id.values(),
                key=lambda item: (
                    int(item.get("turn_start") or 0),
                    int(item.get("turn_end") or 0),
                ),
            )[:limit]
            turns: list[dict[str, Any]] = []
            if not ordered_arcs and not global_node and cutoff is not None:
                turns = [
                    {
                        "turn_no": row.get("turn_no"),
                        "summary": row.get("summary", ""),
                    }
                    for row in self.store.list_archived_turn_summaries(
                        thread_id, cutoff, limit=5
                    )
                ]
            return {
                "global_summary": (
                    global_node.get("summary", "") if global_node else ""
                ),
                "arcs": ordered_arcs,
                "turns": turns,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "主线召回失败",
                extra={"event": "api2.plot.failed", "error": str(exc)},
            )
            return {"global_summary": "", "arcs": [], "turns": []}

    def _context_recall(
        self,
        thread_id: str,
        query: str,
        vector: Sequence[float] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return self.tools.context_recall_vector(
                thread_id=thread_id,
                query=query,
                k=self.settings.context_recall_top_k,
                vector=vector,
            ).get("candidates", [])
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "上下文召回失败",
                extra={"event": "api2.context.failed", "error": str(exc)},
            )
            return []

    def _previous_turn(
        self,
        recent: Sequence[dict[str, Any]],
        turn_no: int | None,
    ) -> dict[str, Any] | None:
        target = int(turn_no) - 1 if turn_no else None
        if target is None:
            turns = [
                int(message.get("turn_no") or 0)
                for message in recent
                if int(message.get("turn_no") or 0) > 0
            ]
            target = max(turns) if turns else None
        if not target or target <= 0:
            return None
        messages = [
            message
            for message in recent
            if int(message.get("turn_no") or 0) == target
        ]
        if not messages:
            return None
        text = "\n".join(
            f"[{'玩家' if message['role'] == 'user' else 'GM'}] "
            f"{message['content']}"
            for message in messages
        )
        return {
            "turn_no": target,
            "text": text,
            "message_ids": [str(message["id"]) for message in messages],
        }

    def _analyze(
        self,
        thread_id: str,
        user_message: str,
        recent: Sequence[dict[str, Any]],
        previous: dict[str, Any] | None,
    ) -> AP2AnalysisResult:
        """两路并行：单轮总结+查询改写；5 轮上下文实体假设。"""
        recent_text = "\n".join(
            f"[{'玩家' if message['role'] == 'user' else 'GM'}] "
            f"{message['content']}"
            for message in recent
        )
        previous_text = previous["text"] if previous else "（无）"
        with ThreadPoolExecutor(max_workers=2) as executor:
            context_future = executor.submit(
                self._analyze_context, user_message, previous_text
            )
            entities_future = executor.submit(
                self._analyze_entities,
                thread_id,
                user_message,
                recent_text,
            )
            context = context_future.result()
            entities = entities_future.result()
        return AP2AnalysisResult(
            context_summary=context.context_summary,
            rewritten_query=context.rewritten_query or user_message,
            entities=_dedupe(entities.entities),
        )

    def _analyze_context(
        self, user_message: str, previous_text: str
    ) -> AP2ContextResult:
        system, user_template = load_prompt("api2_context.md")
        user_prompt = render_prompt(
            user_template,
            previous_turn=previous_text,
            user_message=user_message,
        )
        try:
            return self.llm.chat_json_model(
                AP2ContextResult,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=400,
                timeout=self.settings.dialogue_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "单轮总结/查询改写失败，降级为原文查询",
                extra={"event": "api2.context.failed", "error": str(exc)},
            )
            fallback = self.query_enhancer.analyze(user_message, ())
            return AP2ContextResult(
                context_summary="",
                rewritten_query=fallback.rewritten_query or user_message,
            )

    def _analyze_entities(
        self, thread_id: str, user_message: str, recent_text: str
    ) -> AP2EntitiesResult:
        system, user_template = load_prompt("api2_entities.md")
        user_prompt = render_prompt(
            user_template,
            user_message=user_message,
            recent_context=recent_text or "（无）",
        )
        try:
            return self.llm.chat_json_model(
                AP2EntitiesResult,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=300,
                timeout=self.settings.dialogue_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "实体假设失败，本轮回退为空",
                extra={
                    "event": "api2.entities.failed",
                    "thread_id": thread_id,
                    "error": str(exc),
                },
            )
            return AP2EntitiesResult()

    def _load_details(
        self, thread_id: str, candidates: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for candidate in candidates:
            entity_id = candidate.get("entity_id")
            if not entity_id:
                continue
            try:
                result = self.tools.entity_get(
                    thread_id=thread_id, entity_id=entity_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "实体详情读取失败",
                    extra={
                        "event": "api2.entity.detail.failed",
                        "entity_id": entity_id,
                        "error": str(exc),
                    },
                )
                continue
            if not result.get("found"):
                continue
            entity = result.get("entity", {})
            details.append(
                {
                    "entity_id": entity.get("entity_id", entity_id),
                    "canonical_name": entity.get("canonical_name")
                    or candidate.get("canonical_name"),
                    "entity_type": entity.get("entity_type")
                    or candidate.get("entity_type"),
                    "aliases": entity.get("aliases", candidate.get("aliases", [])),
                    "summary": entity.get("summary", ""),
                    "attributes": result.get("attributes", []),
                    "relations": result.get("relations", []),
                    "rrf_score": candidate.get("rrf_score", 0.0),
                    "routes": candidate.get("routes", []),
                    "route_scores": candidate.get("route_scores", {}),
                }
            )
        return details


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _exclude_recent_context(
    candidates: Sequence[dict[str, Any]],
    recent_turns: set[int],
) -> list[dict[str, Any]]:
    """去掉完全落在最近原始上下文窗口内的召回片段。"""
    if not recent_turns:
        return list(candidates)
    low = min(recent_turns)
    high = max(recent_turns)
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        turn_start = candidate.get("turn_start")
        turn_end = candidate.get("turn_end")
        if (
            turn_start is not None
            and turn_end is not None
            and int(turn_start) >= low
            and int(turn_end) <= high
        ):
            continue
        filtered.append(candidate)
    return filtered


def _dedupe_contexts_by_parent(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同一父块的多个子块只保留得分最高的代表，并记录 siblings。"""
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate.get("parent_no") or candidate.get("chunk_no")
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            item = dict(candidate)
            item["child_chunk_nos"] = [
                value
                for value in [candidate.get("chunk_no")]
                if value
            ]
            item["child_hits"] = 1
            merged[key] = item
            continue

        existing["child_chunk_nos"].append(candidate.get("chunk_no"))
        existing["child_hits"] = existing.get("child_hits", 1) + 1
        if float(candidate.get("score", 0.0)) > float(
            existing.get("score", 0.0)
        ):
            siblings = existing["child_chunk_nos"]
            hits = existing["child_hits"]
            item = dict(candidate)
            item["child_chunk_nos"] = siblings
            item["child_hits"] = hits
            merged[key] = item

    return sorted(
        merged.values(),
        key=lambda item: float(item.get("score", 0.0)),
        reverse=True,
    )


def _fuse_entities(
    keyword_candidates: Sequence[dict[str, Any]],
    vector_candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for route, candidates in (
        ("keyword", keyword_candidates),
        ("vector", vector_candidates),
    ):
        for rank, item in enumerate(candidates, start=1):
            entity_id = item.get("entity_id") or item.get("id")
            if not entity_id:
                continue
            entry = merged.setdefault(
                str(entity_id),
                {
                    "entity_id": str(entity_id),
                    "canonical_name": item.get("canonical_name"),
                    "entity_type": item.get("entity_type"),
                    "aliases": item.get("aliases", []),
                    "matched_field": item.get("matched_field"),
                    "routes": [],
                    "route_scores": {},
                    "rrf_score": 0.0,
                },
            )
            if route not in entry["routes"]:
                entry["routes"].append(route)
            entry["route_scores"][route] = float(
                item.get("route_score", item.get("score", 0.0))
            )
            entry["rrf_score"] += 1.0 / (60 + rank)
            if not entry.get("canonical_name"):
                entry["canonical_name"] = item.get("canonical_name")
            if not entry.get("entity_type"):
                entry["entity_type"] = item.get("entity_type")
            if not entry.get("aliases"):
                entry["aliases"] = item.get("aliases", [])

    return sorted(
        merged.values(), key=lambda item: item["rrf_score"], reverse=True
    )
