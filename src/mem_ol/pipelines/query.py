from __future__ import annotations

import uuid
from typing import Any

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.services.embedding import EmbeddingService, get_embedding_service
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class RetrievalService:
    """最小可用召回：query 向量检索子块，返回父块上下文并写入轨迹。"""

    def __init__(
        self,
        store: PostgresStore,
        embedder: EmbeddingService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder or get_embedding_service()
        self.settings = settings or get_settings()

    def retrieve(
        self,
        thread_id: uuid.UUID | str,
        query: str,
        k: int | None = None,
        *,
        turn_no: int | None = None,
    ) -> list[dict[str, Any]]:
        k = k or self.settings.retrieval_top_k
        query_vector = self.embedder.embed_query(query)
        results = self.store.search_chunks(
            thread_id, query_vector, k=k, level="child"
        )

        trace = [
            {
                "id": str(result["id"]),
                "chunk_no": result["chunk_no"],
                "batch_id": str(result["batch_id"]),
                "score": float(result["score"]),
            }
            for result in results
        ]
        self.store.insert_retrieval_trace(
            thread_id, query, trace, turn_no=turn_no
        )

        with log_context(thread_id=str(thread_id)):
            logger.info(
                "召回完成",
                extra={
                    "event": "query.retrieve.done",
                    "k": k,
                    "hit_count": len(results),
                    "chunk_nos": [item["chunk_no"] for item in trace],
                },
            )
        return results
