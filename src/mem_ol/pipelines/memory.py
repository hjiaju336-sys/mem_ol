from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Sequence

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.pipelines.chunking import SemanticChunker, SourceSegment
from mem_ol.services.embedding import EmbeddingService, get_embedding_service
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)

_ROLE_LABELS = {"user": "用户", "assistant": "GM", "system": "系统"}


def build_dialogue_text(
    messages: Sequence[dict[str, Any]],
) -> tuple[str, list[SourceSegment]]:
    """把一批消息拼成带角色标记的文本，并记录每条消息的字符区间。"""
    parts: list[str] = []
    segments: list[SourceSegment] = []
    offset = 0

    for message in messages:
        label = _ROLE_LABELS.get(message["role"], message["role"])
        prefix = f"【第{message['turn_no']}轮 · {label}】"
        content = message["content"].strip()
        block = f"{prefix}{content}\n\n"

        start = offset
        parts.append(block)
        offset += len(block)
        segments.append(
            SourceSegment(
                start=start,
                end=offset,
                message_id=str(message["id"]),
                turn_no=int(message["turn_no"]),
                role=message["role"],
            )
        )

    return "".join(parts), segments


class MemoryPipeline:
    """对话/世界观的增量向量化入库。"""

    def __init__(
        self,
        store: PostgresStore,
        embedder: EmbeddingService | None = None,
        settings: Settings | None = None,
        chunker: SemanticChunker | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.embedder = embedder or get_embedding_service()
        self.chunker = chunker or SemanticChunker(
            self.embedder.embed,
            parent_size=self.settings.parent_chunk_size,
            parent_min=self.settings.parent_chunk_min,
            parent_max=self.settings.parent_chunk_max,
            child_size=self.settings.child_chunk_size,
            child_min=self.settings.child_chunk_min,
            child_max=self.settings.child_chunk_max,
            child_overlap=self.settings.child_overlap,
            semantic_threshold=self.settings.semantic_threshold,
            max_unit_size=self.settings.chunk_unit_size,
        )

    def ingest_pending(
        self, thread_id: uuid.UUID | str
    ) -> list[dict[str, Any]]:
        """把攒满 N 轮（用户+GM）的未入库消息逐批向量化。"""
        processed: list[dict[str, Any]] = []
        while True:
            messages = self.store.get_un_ingested_messages(thread_id)
            turns = self._complete_turns(messages)
            if len(turns) < self.settings.memory_batch_turns:
                break

            selected = turns[: self.settings.memory_batch_turns]
            selected_messages = [
                message for _, turn_messages in selected for message in turn_messages
            ]
            text, segments = build_dialogue_text(selected_messages)
            batch = self.store.create_batch(
                thread_id,
                kind="dialogue",
                turn_start=selected[0][0],
                turn_end=selected[-1][0],
                char_count=len(text),
            )
            self.store.attach_messages(
                thread_id,
                [message["id"] for message in selected_messages],
                batch["id"],
            )

            try:
                self._process_batch(
                    batch, text, segments, messages=selected_messages
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "记忆批次失败",
                    extra={
                        "event": "memory.batch.failed",
                        "thread_id": str(thread_id),
                        "batch_id": str(batch["id"]),
                        "error": str(exc),
                    },
                )
                self.store.update_batch_status(
                    batch["id"], "failed", error=str(exc)
                )
            else:
                processed.append(self.store.get_batch(batch["id"]) or batch)

        return processed

    def ingest_worldview(
        self,
        thread_id: uuid.UUID | str,
        text: str,
        source_name: str | None = None,
        *,
        doc_id: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> dict[str, Any]:
        """把一段世界观文本作为一个独立批次入库。"""
        text = text.strip()
        if not text:
            raise ValueError("世界观文本为空")

        batch = self.store.create_batch(
            thread_id,
            kind="worldview",
            char_count=len(text),
            doc_id=doc_id,
            char_start=char_start,
            char_end=char_end,
        )
        with log_context(
            thread_id=str(thread_id),
            batch_id=str(batch["id"]),
        ):
            logger.info(
                "世界观批次入库开始",
                extra={
                    "event": "memory.worldview.start",
                    "source": source_name,
                    "char_count": len(text),
                },
            )
        try:
            self._process_batch(batch, text, [])
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "世界观批次失败",
                extra={
                    "event": "memory.worldview.failed",
                    "error": str(exc),
                },
            )
            self.store.update_batch_status(
                batch["id"], "failed", error=str(exc)
            )
            raise
        return self.store.get_batch(batch["id"]) or batch

    def _process_batch(
        self,
        batch: dict[str, Any],
        text: str,
        segments: Sequence[SourceSegment],
        messages: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        batch_id = batch["id"]
        thread_id = batch["thread_id"]
        batch_seq = int(batch["batch_seq"])

        with log_context(
            thread_id=str(thread_id),
            batch_id=str(batch_id),
        ):
            self.store.update_batch_status(batch_id, "processing")
            if messages:
                # 按 turn 边界分别切片，chunk 不跨轮。
                grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for message in messages:
                    grouped[int(message["turn_no"])].append(message)
                chunks = []
                for turn_no in sorted(grouped):
                    turn_messages = sorted(
                        grouped[turn_no],
                        key=lambda message: (
                            0 if message["role"] == "user" else 1
                        ),
                    )
                    turn_text, turn_segments = build_dialogue_text(
                        turn_messages
                    )
                    chunks.extend(
                        self.chunker.split(
                            turn_text,
                            turn_segments,
                            batch_seq=batch_seq,
                            turn_no=turn_no,
                        )
                    )
                for seq_no, chunk in enumerate(chunks, start=1):
                    chunk.seq_no = seq_no
            else:
                chunks = self.chunker.split(
                    text, segments, batch_seq=batch_seq
                )

            # 检索只查子块，父块只用于展开上下文，不需要向量。
            child_positions = [
                index
                for index, chunk in enumerate(chunks)
                if chunk.level == "child"
            ]
            vectors: dict[int, list[float]] = {}
            if child_positions:
                child_vectors = self.embedder.embed(
                    [chunks[index].content for index in child_positions]
                )
                if len(child_vectors) != len(child_positions):
                    raise ValueError("embedding 数量与 chunk 数量不一致")
                vectors = dict(zip(child_positions, child_vectors))

            parent_ids = {
                (chunk.turn_no, chunk.parent_index): uuid.uuid4()
                for chunk in chunks
                if chunk.level == "parent"
            }

            rows: list[dict[str, Any]] = []
            for index, chunk in enumerate(chunks):
                embedding = vectors.get(index)
                parent_key = (chunk.turn_no, chunk.parent_index)
                parent_id = (
                    None
                    if chunk.level == "parent"
                    else parent_ids[parent_key]
                )
                chunk_id = (
                    parent_ids[parent_key]
                    if chunk.level == "parent"
                    else uuid.uuid4()
                )
                rows.append(
                    {
                        "id": chunk_id,
                        "thread_id": thread_id,
                        "batch_id": batch_id,
                        "parent_id": parent_id,
                        "level": chunk.level,
                        "chunk_no": chunk.chunk_no,
                        "seq_no": chunk.seq_no,
                        "source": batch["kind"],
                        "turn_no": chunk.turn_no,
                        "content": chunk.content,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "turn_start": chunk.turn_start,
                        "turn_end": chunk.turn_end,
                        "message_ids": chunk.message_ids,
                        "embedding": embedding,
                    }
                )

            self.store.delete_chunks_for_batch(batch_id)
            self.store.insert_chunks(rows)
            self.store.update_batch_status(
                batch_id, "done", chunk_count=len(rows)
            )
            logger.info(
                "记忆批次入库完成",
                extra={
                    "event": "memory.batch.done",
                    "batch_seq": batch_seq,
                    "kind": batch["kind"],
                    "chunk_count": len(rows),
                    "parent_count": sum(
                        1 for chunk in chunks if chunk.level == "parent"
                    ),
                    "child_count": sum(
                        1 for chunk in chunks if chunk.level == "child"
                    ),
                },
            )

    @staticmethod
    def _complete_turns(
        messages: Sequence[dict[str, Any]],
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for message in messages:
            grouped[int(message["turn_no"])].append(message)

        turns: list[tuple[int, list[dict[str, Any]]]] = []
        for turn_no in sorted(grouped):
            turn_messages = grouped[turn_no]
            roles = {message["role"] for message in turn_messages}
            if "user" not in roles or "assistant" not in roles:
                continue
            turn_messages.sort(
                key=lambda message: 0 if message["role"] == "user" else 1
            )
            turns.append((turn_no, turn_messages))
        return turns
