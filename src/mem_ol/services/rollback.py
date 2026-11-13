from __future__ import annotations

import uuid
from typing import Any

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.storage.neo4j import Neo4jStore
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class RollbackService:
    """回退到指定轮次：标记消息、推进水位线、物理删除后续记忆。"""

    def __init__(
        self,
        store: PostgresStore,
        neo4j: Neo4jStore,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.neo4j = neo4j
        self.settings = settings or get_settings()

    def rollback(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> dict[str, Any]:
        tid = str(thread_id)
        target_turn = int(target_turn)
        if target_turn < 0:
            raise ValueError("target_turn 必须 >= 0")
        if self.store.get_thread(tid) is None:
            raise ValueError(f"thread 不存在: {tid}")

        watermark = self.store.get_target_turn_watermark(
            tid, target_turn
        )
        if watermark is None:
            raise ValueError(
                f"目标轮次不存在或已回退: turn {target_turn}"
            )

        checkpoint_id = self.store.get_target_checkpoint(
            tid, target_turn
        )

        with log_context(thread_id=tid):
            rolled_back = self.store.mark_messages_rolled_back(
                tid, target_turn
            )
            chunks = self.store.delete_chunks_after_turn(
                tid, target_turn
            )
            entities = self.store.delete_entity_index_after_turn(
                tid, target_turn
            )
            mentions = self.store.delete_mentions_after_turn(
                tid, target_turn
            )
            traces = self.store.delete_traces_after_turn(
                tid, target_turn
            )
            plot_nodes = self.store.delete_plot_nodes_after_turn(
                tid, target_turn
            )
            graph = self.neo4j.rollback_after_turn(tid, target_turn)
            self.store.refresh_batch_chunk_counts(tid)
            self.store.upsert_thread_state(tid, watermark)

            summary = {
                "thread_id": tid,
                "target_turn": target_turn,
                "watermark": watermark.isoformat(),
                "checkpoint_id": checkpoint_id,
                "rolled_back_messages": rolled_back,
                "deleted_chunks": chunks,
                "deleted_entity_index": entities,
                "deleted_mentions": mentions,
                "deleted_traces": traces,
                "deleted_plot_nodes": plot_nodes,
                "deleted_graph_attributes": graph.get("attributes", 0),
                "deleted_graph_relations": graph.get("relations", 0),
                "deleted_graph_entities": graph.get("entities", 0),
            }
            logger.info(
                "回退完成",
                extra={
                    "event": "rollback.done",
                    **{
                        key: summary[key]
                        for key in (
                            "target_turn",
                            "deleted_chunks",
                            "deleted_entity_index",
                            "deleted_graph_attributes",
                            "deleted_graph_relations",
                            "deleted_graph_entities",
                        )
                    },
                },
            )
            return summary
