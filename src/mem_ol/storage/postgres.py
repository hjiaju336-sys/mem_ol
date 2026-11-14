from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_schema(dsn: str, schema_path: Path | str) -> None:
    """建表；schema.sql 中只有普通 DDL，按分号拆分执行。"""
    sql = Path(schema_path).read_text(encoding="utf-8")
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    cur.execute(statement)


class PostgresStore:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 4) -> None:
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            configure=register_vector,
            kwargs={"row_factory": dict_row},
        )

    def close(self) -> None:
        self.pool.close()

    # ---------- threads ----------

    def create_thread(self, name: str) -> dict[str, Any]:
        thread_id = uuid.uuid4()
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO threads (id, name) VALUES (%s, %s) RETURNING *",
                (thread_id, name),
            ).fetchone()
            conn.commit()
        return row

    def get_or_create_thread(self, name: str) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE name = %s ORDER BY created_at LIMIT 1",
                (name,),
            ).fetchone()
            if row is None:
                thread_id = uuid.uuid4()
                row = conn.execute(
                    "INSERT INTO threads (id, name) VALUES (%s, %s) RETURNING *",
                    (thread_id, name),
                ).fetchone()
                conn.commit()
        return row

    def get_thread(self, thread_id: uuid.UUID | str) -> dict[str, Any] | None:
        tid = _as_uuid(thread_id)
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM threads WHERE id = %s", (tid,)
            ).fetchone()

    def list_threads(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM threads ORDER BY created_at DESC"
            ).fetchall()

    # ---------- messages ----------

    def add_message(
        self,
        thread_id: uuid.UUID | str,
        role: str,
        content: str,
        turn_no: int,
        occurred_at: datetime | None = None,
        batch_id: uuid.UUID | str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = uuid.uuid4()
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (
                    id, thread_id, role, content, turn_no, occurred_at,
                    batch_id, checkpoint_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    message_id,
                    _as_uuid(thread_id),
                    role,
                    content,
                    turn_no,
                    occurred_at or _utcnow(),
                    _as_uuid(batch_id) if batch_id else None,
                    checkpoint_id,
                ),
            ).fetchone()
            conn.commit()
        return row

    def get_recent_turns(
        self, thread_id: uuid.UUID | str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """取最近 limit 个轮次的全部消息，不区分是否已入库。"""
        if limit <= 0:
            return []
        with self.pool.connection() as conn:
            return conn.execute(
                """
                WITH recent_turns AS (
                    SELECT DISTINCT turn_no
                    FROM messages
                    WHERE thread_id = %s AND turn_no > 0 AND rolled_back = false
                    ORDER BY turn_no DESC
                    LIMIT %s
                )
                SELECT m.*
                FROM messages m
                WHERE m.thread_id = %s AND m.rolled_back = false
                  AND (
                      m.turn_no IN (SELECT turn_no FROM recent_turns)
                      OR m.turn_no = 0
                  )
                ORDER BY m.turn_no ASC,
                         CASE m.role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END,
                         m.occurred_at
                """,
                (_as_uuid(thread_id), int(limit), _as_uuid(thread_id)),
            ).fetchall()

    def get_un_ingested_messages(
        self, thread_id: uuid.UUID | str
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM messages
                WHERE thread_id = %s AND batch_id IS NULL AND rolled_back = false
                ORDER BY turn_no,
                         CASE role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END,
                         occurred_at
                """,
                (_as_uuid(thread_id),),
            ).fetchall()

    def cleanup_checkpoints(
        self,
        thread_id: uuid.UUID | str,
        keep_turns: int = 3,
    ) -> dict[str, int]:
        """只保留最近 keep_turns 轮的 LangGraph checkpoint。

        checkpoint 链通过 parent_checkpoint_id 回溯；删除旧 checkpoint、
        对应 writes，以及未被保留 checkpoint 引用的 blobs。旧消息的
        checkpoint_id 置空，避免指向已删除的 checkpoint。
        """
        if keep_turns <= 0:
            return {"deleted_checkpoints": 0, "deleted_writes": 0, "deleted_blobs": 0}
        tid = _as_uuid(thread_id)
        tid_text = str(tid)
        with self.pool.connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT DISTINCT ON (turn_no) turn_no, checkpoint_id
                    FROM messages
                    WHERE thread_id = %s AND checkpoint_id IS NOT NULL
                    ORDER BY turn_no DESC
                    LIMIT %s
                    """,
                    (tid, int(keep_turns)),
                ).fetchall()
                if len(rows) < keep_turns:
                    return {
                        "deleted_checkpoints": 0,
                        "deleted_writes": 0,
                        "deleted_blobs": 0,
                    }

                keep_ids = [row["checkpoint_id"] for row in rows]
                cutoff_turn = min(int(row["turn_no"]) for row in rows)
                conn.execute(
                    """
                    UPDATE messages SET checkpoint_id = NULL
                    WHERE thread_id = %s AND turn_no < %s
                    """,
                    (tid, cutoff_turn),
                )

                # 只保留最近 keep_turns 轮的 checkpoint。
                # 注意：不能从最新 checkpoint 递归回溯 parent，否则会保留上一轮。
                conn.execute(
                    """
                    CREATE TEMP TABLE keep_cp ON COMMIT DROP AS
                    SELECT checkpoint_id, checkpoint_ns, checkpoint
                    FROM checkpoints
                    WHERE thread_id = %s
                      AND (checkpoint->'channel_values'->>'turn_no') IS NOT NULL
                      AND (checkpoint->'channel_values'->>'turn_no')::int >= %s
                    """,
                    (tid_text, cutoff_turn),
                )
                conn.execute(
                    """
                    CREATE TEMP TABLE keep_ver ON COMMIT DROP AS
                    SELECT DISTINCT value AS version
                    FROM keep_cp,
                         jsonb_each_text(keep_cp.checkpoint->'channel_versions')
                    """
                )

                # 被保留 checkpoint 的 parent 如果落在删除区间，置空避免悬挂指针。
                conn.execute(
                    """
                    UPDATE checkpoints c SET parent_checkpoint_id = NULL
                    WHERE c.thread_id = %s
                      AND c.parent_checkpoint_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM keep_cp k
                          WHERE k.checkpoint_id = c.parent_checkpoint_id
                            AND k.checkpoint_ns = c.checkpoint_ns
                      )
                    """,
                    (tid_text,),
                )
                deleted_writes = conn.execute(
                    """
                    DELETE FROM checkpoint_writes w
                    WHERE w.thread_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM keep_cp k
                          WHERE k.checkpoint_id = w.checkpoint_id
                            AND k.checkpoint_ns = w.checkpoint_ns
                      )
                    """,
                    (tid_text,),
                ).rowcount
                deleted_blobs = conn.execute(
                    """
                    DELETE FROM checkpoint_blobs b
                    WHERE b.thread_id = %s
                      AND EXISTS (SELECT 1 FROM keep_ver)
                      AND b.version NOT IN (SELECT version FROM keep_ver)
                    """,
                    (tid_text,),
                ).rowcount
                deleted_checkpoints = conn.execute(
                    """
                    DELETE FROM checkpoints c
                    WHERE c.thread_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM keep_cp k
                          WHERE k.checkpoint_id = c.checkpoint_id
                            AND k.checkpoint_ns = c.checkpoint_ns
                      )
                    """,
                    (tid_text,),
                ).rowcount

        return {
            "deleted_checkpoints": int(deleted_checkpoints),
            "deleted_writes": int(deleted_writes),
            "deleted_blobs": int(deleted_blobs),
        }

    def update_turn_checkpoint(
        self,
        thread_id: uuid.UUID | str,
        turn_no: int,
        checkpoint_id: str,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE messages SET checkpoint_id = %s
                WHERE thread_id = %s AND turn_no = %s
                """,
                (checkpoint_id, _as_uuid(thread_id), turn_no),
            )
            conn.commit()

    def get_opening(
        self, thread_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM messages
                WHERE thread_id = %s AND turn_no = 0 AND role = 'assistant'
                ORDER BY occurred_at
                LIMIT 1
                """,
                (_as_uuid(thread_id),),
            ).fetchone()

    def count_un_ingested_turns(self, thread_id: uuid.UUID | str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS turns FROM (
                    SELECT turn_no
                    FROM messages
                    WHERE thread_id = %s AND batch_id IS NULL
                      AND rolled_back = false
                    GROUP BY turn_no
                    HAVING COUNT(*) FILTER (WHERE role = 'user') > 0
                       AND COUNT(*) FILTER (WHERE role = 'assistant') > 0
                ) complete_turns
                """,
                (_as_uuid(thread_id),),
            ).fetchone()
        return int(row["turns"])

    def next_turn_no(self, thread_id: uuid.UUID | str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_no), 0) + 1 AS next_turn FROM messages WHERE thread_id = %s",
                (_as_uuid(thread_id),),
            ).fetchone()
        return int(row["next_turn"])

    def list_messages(
        self,
        thread_id: uuid.UUID | str,
        *,
        include_rolled_back: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, thread_id, role, content, turn_no, occurred_at,
                   batch_id, checkpoint_id, rolled_back
            FROM messages
            WHERE thread_id = %s
        """
        params: list[Any] = [_as_uuid(thread_id)]
        if not include_rolled_back:
            sql += " AND rolled_back = false"
        sql += """
            ORDER BY turn_no,
                     CASE role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END,
                     occurred_at
        """
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def attach_messages(
        self, thread_id: uuid.UUID | str, message_ids: Sequence[uuid.UUID | str], batch_id: uuid.UUID | str
    ) -> None:
        if not message_ids:
            return
        ids = [_as_uuid(mid) for mid in message_ids]
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE messages SET batch_id = %s WHERE thread_id = %s AND id = ANY(%s)",
                (_as_uuid(batch_id), _as_uuid(thread_id), ids),
            )
            conn.commit()

    # ---------- ingestion batches ----------

    def create_batch(
        self,
        thread_id: uuid.UUID | str,
        kind: str,
        turn_start: int | None = None,
        turn_end: int | None = None,
        char_count: int = 0,
        doc_id: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> dict[str, Any]:
        tid = _as_uuid(thread_id)
        batch_id = uuid.uuid4()
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT id FROM threads WHERE id = %s FOR UPDATE", (tid,)
                ).fetchone()
                seq_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(batch_seq), 0) + 1 AS next_seq
                    FROM ingestion_batches WHERE thread_id = %s
                    """,
                    (tid,),
                ).fetchone()
                batch_seq = int(seq_row["next_seq"])
                row = conn.execute(
                    """
                    INSERT INTO ingestion_batches (
                        id, thread_id, batch_seq, kind, turn_start, turn_end,
                        char_count, doc_id, char_start, char_end
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        batch_id,
                        tid,
                        batch_seq,
                        kind,
                        turn_start,
                        turn_end,
                        char_count,
                        doc_id,
                        char_start,
                        char_end,
                    ),
                ).fetchone()
        return row

    def update_batch_status(
        self,
        batch_id: uuid.UUID | str,
        status: str,
        chunk_count: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        bid = _as_uuid(batch_id)
        if status == "done":
            sql = """
                UPDATE ingestion_batches
                SET status = %s, chunk_count = COALESCE(%s, chunk_count),
                    error = %s, processed_at = now()
                WHERE id = %s RETURNING *
            """
        else:
            sql = """
                UPDATE ingestion_batches
                SET status = %s, chunk_count = COALESCE(%s, chunk_count),
                    error = %s
                WHERE id = %s RETURNING *
            """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (status, chunk_count, error, bid)).fetchone()
            conn.commit()
        return row

    def get_batch(self, batch_id: uuid.UUID | str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM ingestion_batches WHERE id = %s",
                (_as_uuid(batch_id),),
            ).fetchone()

    def list_batches(self, thread_id: uuid.UUID | str) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM ingestion_batches
                WHERE thread_id = %s ORDER BY batch_seq
                """,
                (_as_uuid(thread_id),),
            ).fetchall()

    # ---------- chunks ----------

    def delete_chunks_for_batch(self, batch_id: uuid.UUID | str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE batch_id = %s", (_as_uuid(batch_id),)
            )
            conn.commit()

    def insert_chunks(self, chunks: Sequence[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        sql = """
            INSERT INTO chunks (
                id, thread_id, batch_id, parent_id, level, chunk_no, seq_no,
                source, turn_no, content, char_start, char_end,
                turn_start, turn_end, message_ids, embedding
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
        """

        def row_values(chunk: dict[str, Any]) -> tuple[Any, ...]:
            return (
                chunk["id"],
                chunk["thread_id"],
                chunk["batch_id"],
                chunk.get("parent_id"),
                chunk["level"],
                chunk["chunk_no"],
                chunk["seq_no"],
                chunk["source"],
                chunk.get("turn_no"),
                chunk["content"],
                chunk["char_start"],
                chunk["char_end"],
                chunk.get("turn_start"),
                chunk.get("turn_end"),
                Jsonb(chunk.get("message_ids", [])),
                Vector(chunk["embedding"]) if chunk.get("embedding") is not None else None,
            )

        parents = [c for c in chunks if c["level"] == "parent"]
        children = [c for c in chunks if c["level"] == "child"]

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                if parents:
                    cur.executemany(sql, [row_values(c) for c in parents])
                if children:
                    cur.executemany(sql, [row_values(c) for c in children])
            conn.commit()
        return len(chunks)

    def list_chunks(
        self,
        thread_id: uuid.UUID | str,
        batch_id: uuid.UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT c.id, c.thread_id, c.batch_id, c.parent_id, c.level,
                   c.chunk_no, c.seq_no, c.source, c.turn_no, c.content,
                   c.char_start, c.char_end, c.turn_start, c.turn_end,
                   c.message_ids,
                   (c.embedding IS NOT NULL) AS has_embedding,
                   length(c.content) AS content_length, c.created_at,
                   b.batch_seq
            FROM chunks c
            JOIN ingestion_batches b ON b.id = c.batch_id
            WHERE c.thread_id = %s
        """
        params: list[Any] = [_as_uuid(thread_id)]
        if batch_id is not None:
            sql += " AND c.batch_id = %s"
            params.append(_as_uuid(batch_id))
        sql += " ORDER BY b.batch_seq, c.seq_no"
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    # ---------- rollback ----------

    def get_target_turn_watermark(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> datetime | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT MAX(occurred_at) AS watermark
                FROM messages
                WHERE thread_id = %s AND turn_no <= %s
                  AND rolled_back = false
                """,
                (_as_uuid(thread_id), int(target_turn)),
            ).fetchone()
        return row["watermark"] if row else None

    def get_target_checkpoint(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT checkpoint_id FROM messages
                WHERE thread_id = %s AND turn_no <= %s
                  AND checkpoint_id IS NOT NULL
                ORDER BY turn_no DESC
                LIMIT 1
                """,
                (_as_uuid(thread_id), int(target_turn)),
            ).fetchone()
        return row["checkpoint_id"] if row else None

    def mark_messages_rolled_back(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE messages SET rolled_back = true
                WHERE thread_id = %s AND turn_no > %s
                  AND rolled_back = false
                """,
                (_as_uuid(thread_id), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def upsert_thread_state(
        self,
        thread_id: uuid.UUID | str,
        watermark: datetime,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO thread_states (
                    thread_id, rollback_watermark, version, updated_at
                ) VALUES (%s, %s, 1, now())
                ON CONFLICT (thread_id) DO UPDATE SET
                    rollback_watermark = COALESCE(
                        GREATEST(
                            thread_states.rollback_watermark,
                            EXCLUDED.rollback_watermark
                        ),
                        EXCLUDED.rollback_watermark
                    ),
                    version = thread_states.version + 1,
                    updated_at = now()
                """,
                (_as_uuid(thread_id), watermark),
            )
            conn.commit()

    def get_game_state(self, thread_id: uuid.UUID | str) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT state FROM game_states WHERE thread_id = %s",
                (_as_uuid(thread_id),),
            ).fetchone()
        return row["state"] if row else {}

    def save_game_state(
        self,
        thread_id: uuid.UUID | str,
        turn_no: int,
        state: dict[str, Any],
        *,
        kind: str = "state",
        payload: dict[str, Any] | None = None,
    ) -> None:
        tid = _as_uuid(thread_id)
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO game_states (thread_id, state, version, updated_at)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (thread_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    version = game_states.version + 1,
                    updated_at = now()
                """,
                (tid, Jsonb(state)),
            )
            conn.execute(
                """
                INSERT INTO state_events (
                    id, thread_id, turn_no, kind, payload, state_after
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4(),
                    tid,
                    int(turn_no),
                    kind,
                    Jsonb(payload or {}),
                    Jsonb(state),
                ),
            )
            conn.commit()

    def delete_state_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        tid = _as_uuid(thread_id)
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM state_events
                WHERE thread_id = %s AND turn_no > %s
                """,
                (tid, int(target_turn)),
            )
            deleted = int(cursor.rowcount)
            row = conn.execute(
                """
                SELECT state_after FROM state_events
                WHERE thread_id = %s AND turn_no <= %s
                ORDER BY turn_no DESC, created_at DESC
                LIMIT 1
                """,
                (tid, int(target_turn)),
            ).fetchone()
            state = row["state_after"] if row else {}
            conn.execute(
                """
                INSERT INTO game_states (thread_id, state, version, updated_at)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (thread_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    version = game_states.version + 1,
                    updated_at = now()
                """,
                (tid, Jsonb(state)),
            )
            conn.commit()
        return deleted

    def delete_chunks_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM chunks
                WHERE thread_id = %s AND turn_no > %s
                """,
                (_as_uuid(thread_id), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def delete_entity_index_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM entity_index
                WHERE thread_id = %s AND created_turn_no > %s
                """,
                (_as_uuid(thread_id), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def delete_mentions_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM entity_mentions
                WHERE thread_id = %s AND turn_no > %s
                """,
                (_as_uuid(thread_id), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def delete_traces_before_turn(
        self, thread_id: uuid.UUID | str, min_turn: int
    ) -> int:
        """轨迹跟轮次上下文一起保留；开场白 turn 0 不删。"""
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM retrieval_traces
                WHERE thread_id = %s AND turn_no > 0 AND turn_no < %s
                """,
                (_as_uuid(thread_id), int(min_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def delete_traces_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM retrieval_traces
                WHERE thread_id = %s AND turn_no > %s
                """,
                (_as_uuid(thread_id), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    def refresh_batch_chunk_counts(self, thread_id: uuid.UUID | str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE ingestion_batches b
                SET chunk_count = (
                    SELECT COUNT(*) FROM chunks c WHERE c.batch_id = b.id
                )
                WHERE b.thread_id = %s
                """,
                (_as_uuid(thread_id),),
            )
            conn.commit()

    # ---------- retrieval ----------

    def search_chunks(
        self,
        thread_id: uuid.UUID | str,
        query_embedding: Sequence[float],
        k: int = 4,
        level: str = "child",
    ) -> list[dict[str, Any]]:
        qvec = Vector(list(query_embedding))
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT c.id, c.batch_id, c.chunk_no, c.content, c.parent_id,
                       c.turn_no, c.turn_start, c.turn_end, c.message_ids,
                       1 - (c.embedding <=> %s) AS score,
                       p.chunk_no AS parent_no, p.content AS parent_content
                FROM chunks c
                LEFT JOIN chunks p ON p.id = c.parent_id
                WHERE c.thread_id = %s
                  AND c.level = %s
                  AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s
                LIMIT %s
                """,
                (qvec, _as_uuid(thread_id), level, qvec, k),
            ).fetchall()

    def list_retrieval_traces(
        self, thread_id: uuid.UUID | str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM retrieval_traces
                WHERE thread_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (_as_uuid(thread_id), limit),
            ).fetchall()

    def insert_retrieval_trace(
        self,
        thread_id: uuid.UUID | str,
        query_text: str,
        chunk_ids: Sequence[dict[str, Any]],
        *,
        turn_no: int | None = None,
    ) -> uuid.UUID:
        trace_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO retrieval_traces (
                    id, thread_id, turn_no, query_text, chunk_ids
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    trace_id,
                    _as_uuid(thread_id),
                    turn_no,
                    query_text,
                    Jsonb(list(chunk_ids)),
                ),
            )
            conn.commit()
        return trace_id

    def insert_retrieval_trace_v2(
        self,
        *,
        trace_id: uuid.UUID | str,
        thread_id: uuid.UUID | str,
        turn_no: int | None,
        query_text: str,
        context_summary: str | None,
        hypothetical_entities: Sequence[dict[str, Any]],
        entity_keyword_candidates: Sequence[dict[str, Any]],
        entity_vector_candidates: Sequence[dict[str, Any]],
        entity_fused_candidates: Sequence[dict[str, Any]],
        context_candidates: Sequence[dict[str, Any]],
        rerank_scores: dict[str, Any] | None = None,
        selected: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        row_id = uuid.uuid4()
        chunk_ids = [
            item.get("chunk_id")
            for item in context_candidates
            if item.get("chunk_id")
        ]
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO retrieval_traces (
                    id, thread_id, trace_id, turn_no, query_text,
                    context_summary, hypothetical_entities,
                    entity_keyword_candidates, entity_vector_candidates,
                    entity_fused_candidates, context_candidates,
                    rerank_scores, selected, chunk_ids
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    row_id,
                    _as_uuid(thread_id),
                    _as_uuid(trace_id),
                    turn_no,
                    query_text,
                    context_summary,
                    Jsonb(_json_safe(hypothetical_entities)),
                    Jsonb(_json_safe(entity_keyword_candidates)),
                    Jsonb(_json_safe(entity_vector_candidates)),
                    Jsonb(_json_safe(entity_fused_candidates)),
                    Jsonb(_json_safe(context_candidates)),
                    Jsonb(_json_safe(rerank_scores or {})),
                    Jsonb(_json_safe(selected or {})),
                    Jsonb(_json_safe(chunk_ids)),
                ),
            )
            conn.commit()
        return _as_uuid(trace_id)

    # ---------- plot nodes: L0 turn / L1 arc / L2 global ----------

    def upsert_turn_summary(
        self,
        thread_id: uuid.UUID | str,
        turn_no: int,
        summary: str,
        message_ids: Sequence[uuid.UUID | str] = (),
    ) -> dict[str, Any]:
        ids = [uuid.UUID(str(item)) for item in message_ids]
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO plot_nodes (
                    id, thread_id, level, turn_no, summary,
                    status, message_ids
                ) VALUES (%s, %s, 'turn', %s, %s, 'active', %s::uuid[])
                ON CONFLICT (thread_id, turn_no) WHERE level = 'turn'
                DO UPDATE SET
                    summary = EXCLUDED.summary,
                    message_ids = EXCLUDED.message_ids,
                    status = 'active',
                    embedding = NULL,
                    updated_at = now()
                RETURNING *
                """,
                (uuid.uuid4(), _as_uuid(thread_id), int(turn_no), summary, ids),
            ).fetchone()
            conn.commit()
        return row

    def list_turn_summaries(
        self, thread_id: uuid.UUID | str
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM plot_nodes
                WHERE thread_id = %s AND level = 'turn'
                ORDER BY turn_no
                """,
                (_as_uuid(thread_id),),
            ).fetchall()

    def list_active_turn_summaries(
        self, thread_id: uuid.UUID | str, max_turn: int
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM plot_nodes
                WHERE thread_id = %s AND level = 'turn'
                  AND status = 'active' AND turn_no <= %s
                ORDER BY turn_no
                """,
                (_as_uuid(thread_id), int(max_turn)),
            ).fetchall()

    def mark_plot_nodes_archived(
        self, nodes: Sequence[dict[str, Any]]
    ) -> int:
        if not nodes:
            return 0
        with self.pool.connection() as conn:
            with conn.transaction():
                for node in nodes:
                    conn.execute(
                        """
                        UPDATE plot_nodes
                        SET embedding = %s, status = 'archived', updated_at = now()
                        WHERE id = %s
                        """,
                        (Vector(list(node["embedding"])), node["id"]),
                    )
        return len(nodes)

    def list_archived_turn_summaries(
        self,
        thread_id: uuid.UUID | str,
        max_turn: int,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM plot_nodes
                WHERE thread_id = %s AND level = 'turn'
                  AND status = 'archived' AND turn_no <= %s
                ORDER BY turn_no DESC
                LIMIT %s
                """,
                (_as_uuid(thread_id), int(max_turn), int(limit)),
            ).fetchall()
        return list(reversed(rows))

    def upsert_arc_summary(
        self,
        thread_id: uuid.UUID | str,
        turn_start: int,
        turn_end: int,
        summary: str,
        embedding: Sequence[float],
    ) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO plot_nodes (
                    id, thread_id, level, turn_start, turn_end, summary,
                    embedding, status
                ) VALUES (%s, %s, 'arc', %s, %s, %s, %s, 'archived')
                ON CONFLICT (thread_id, turn_start, turn_end) WHERE level = 'arc'
                DO UPDATE SET
                    summary = EXCLUDED.summary,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                RETURNING *
                """,
                (
                    uuid.uuid4(),
                    _as_uuid(thread_id),
                    int(turn_start),
                    int(turn_end),
                    summary,
                    Vector(list(embedding)),
                ),
            ).fetchone()
            conn.commit()
        return row

    def list_arcs(
        self,
        thread_id: uuid.UUID | str,
        max_turn_end: int,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM plot_nodes
                WHERE thread_id = %s AND level = 'arc'
                  AND turn_end <= %s
                ORDER BY turn_end DESC
                LIMIT %s
                """,
                (_as_uuid(thread_id), int(max_turn_end), int(limit)),
            ).fetchall()
        return list(reversed(rows))

    def search_arc_summaries(
        self,
        thread_id: uuid.UUID | str,
        query_embedding: Sequence[float],
        max_turn_end: int,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT id, turn_start, turn_end, summary,
                       1 - (embedding <=> %s) AS score
                FROM plot_nodes
                WHERE thread_id = %s AND level = 'arc'
                  AND status = 'archived' AND embedding IS NOT NULL
                  AND turn_end <= %s
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (
                    Vector(list(query_embedding)),
                    _as_uuid(thread_id),
                    int(max_turn_end),
                    Vector(list(query_embedding)),
                    int(limit),
                ),
            ).fetchall()

    def get_global_plot(
        self, thread_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM plot_nodes
                WHERE thread_id = %s AND level = 'global'
                LIMIT 1
                """,
                (_as_uuid(thread_id),),
            ).fetchone()

    def upsert_global_plot(
        self, thread_id: uuid.UUID | str, summary: str
    ) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO plot_nodes (
                    id, thread_id, level, summary, status
                ) VALUES (%s, %s, 'global', %s, 'archived')
                ON CONFLICT (thread_id) WHERE level = 'global'
                DO UPDATE SET
                    summary = EXCLUDED.summary, updated_at = now()
                RETURNING *
                """,
                (uuid.uuid4(), _as_uuid(thread_id), summary),
            ).fetchone()
            conn.commit()
        return row

    def delete_plot_nodes_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM plot_nodes
                WHERE thread_id = %s AND (
                    (level = 'turn' AND turn_no > %s) OR
                    (level = 'arc' AND turn_end > %s) OR
                    level = 'global'
                )
                """,
                (_as_uuid(thread_id), int(target_turn), int(target_turn)),
            )
            conn.commit()
        return int(cursor.rowcount)

    # ---------- entity index (derived from Neo4j) ----------

    def upsert_entity_index(
        self,
        *,
        entity_id: uuid.UUID | str,
        thread_id: uuid.UUID | str,
        canonical_name: str,
        normalized_name: str,
        entity_type: str,
        aliases: Sequence[str],
        embedding_text: str,
        embedding: Sequence[float],
        index_status: str = "ready",
        created_turn_no: int | None = None,
        created_batch_id: uuid.UUID | str | None = None,
        last_turn_no: int | None = None,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO entity_index (
                    entity_id, thread_id, canonical_name, normalized_name,
                    entity_type, aliases, embedding_text, embedding,
                    index_status, created_turn_no, created_batch_id,
                    last_turn_no, last_indexed_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, now(), now()
                )
                ON CONFLICT (entity_id) DO UPDATE SET
                    thread_id = EXCLUDED.thread_id,
                    canonical_name = EXCLUDED.canonical_name,
                    normalized_name = EXCLUDED.normalized_name,
                    entity_type = EXCLUDED.entity_type,
                    aliases = EXCLUDED.aliases,
                    embedding_text = EXCLUDED.embedding_text,
                    embedding = EXCLUDED.embedding,
                    index_status = EXCLUDED.index_status,
                    created_turn_no = COALESCE(
                        entity_index.created_turn_no, EXCLUDED.created_turn_no
                    ),
                    created_batch_id = COALESCE(
                        entity_index.created_batch_id, EXCLUDED.created_batch_id
                    ),
                    last_turn_no = EXCLUDED.last_turn_no,
                    last_indexed_at = now(),
                    updated_at = now()
                """,
                (
                    _as_uuid(entity_id),
                    _as_uuid(thread_id),
                    canonical_name,
                    normalized_name,
                    entity_type,
                    Jsonb(list(aliases)),
                    embedding_text,
                    Vector(list(embedding)),
                    index_status,
                    created_turn_no,
                    _as_uuid(created_batch_id) if created_batch_id else None,
                    last_turn_no,
                ),
            )
            conn.commit()

    def search_entity_vectors(
        self,
        thread_id: uuid.UUID | str,
        query_embedding: Sequence[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        qvec = Vector(list(query_embedding))
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT entity_id, canonical_name, entity_type, aliases,
                       embedding_text,
                       1 - (embedding <=> %s) AS score
                FROM entity_index
                WHERE thread_id = %s AND index_status = 'ready'
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (qvec, _as_uuid(thread_id), qvec, int(limit)),
            ).fetchall()

    def list_entity_index(
        self, thread_id: uuid.UUID | str
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT entity_id, thread_id, canonical_name, normalized_name,
                       entity_type, aliases, embedding_text, index_status,
                       created_turn_no, created_batch_id, last_turn_no,
                       last_indexed_at, updated_at
                FROM entity_index
                WHERE thread_id = %s
                ORDER BY updated_at DESC
                """,
                (_as_uuid(thread_id),),
            ).fetchall()

    def insert_entity_mention(
        self,
        *,
        entity_id: uuid.UUID | str,
        thread_id: uuid.UUID | str,
        batch_id: uuid.UUID | str | None,
        trace_id: uuid.UUID | str | None,
        turn_no: int | None,
        surface_form: str,
        message_ids: Sequence[str],
        chunk_ids: Sequence[str],
        chunk_nos: Sequence[str],
        occurred_at: datetime,
    ) -> uuid.UUID:
        mention_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, thread_id, batch_id, trace_id, turn_no,
                    surface_form, message_ids, chunk_ids, chunk_nos, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trace_id, entity_id, surface_form, turn_no)
                DO NOTHING
                """,
                (
                    mention_id,
                    _as_uuid(entity_id),
                    _as_uuid(thread_id),
                    _as_uuid(batch_id) if batch_id else None,
                    _as_uuid(trace_id) if trace_id else None,
                    turn_no,
                    surface_form,
                    Jsonb(list(message_ids)),
                    Jsonb(list(chunk_ids)),
                    Jsonb(list(chunk_nos)),
                    occurred_at,
                ),
            )
            conn.commit()
        return mention_id

    def list_entity_mentions(
        self, thread_id: uuid.UUID | str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM entity_mentions
                WHERE thread_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (_as_uuid(thread_id), int(limit)),
            ).fetchall()

    def insert_tool_call_log(
        self,
        *,
        tool_name: str,
        thread_id: uuid.UUID | str | None,
        trace_id: uuid.UUID | str | None,
        arguments: dict[str, Any],
        result_summary: dict[str, Any],
        success: bool,
        error: str | None,
        duration_ms: int,
    ) -> uuid.UUID:
        log_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tool_call_logs (
                    id, trace_id, thread_id, tool_name, arguments,
                    result_summary, success, error, duration_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    log_id,
                    _as_uuid(trace_id) if trace_id else None,
                    _as_uuid(thread_id) if thread_id else None,
                    tool_name,
                    Jsonb(_json_safe(arguments)),
                    Jsonb(_json_safe(result_summary)),
                    success,
                    error,
                    duration_ms,
                ),
            )
            conn.commit()
        return log_id

    def insert_entity_index_job(
        self,
        *,
        entity_id: uuid.UUID | str,
        thread_id: uuid.UUID | str,
        reason: str,
        error: str | None = None,
    ) -> uuid.UUID:
        job_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO entity_index_jobs (
                    job_id, entity_id, thread_id, reason, error
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    _as_uuid(entity_id),
                    _as_uuid(thread_id),
                    reason,
                    error,
                ),
            )
            conn.commit()
        return job_id

    def get_chunks_by_nos(
        self, thread_id: uuid.UUID | str, chunk_nos: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not chunk_nos:
            return []
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT c.id, c.batch_id, c.chunk_no, c.parent_id, c.level,
                       c.turn_no, c.content, c.turn_start, c.turn_end,
                       c.message_ids,
                       c.char_start, c.char_end,
                       p.chunk_no AS parent_no, p.content AS parent_content
                FROM chunks c
                LEFT JOIN chunks p ON p.id = c.parent_id
                WHERE c.thread_id = %s AND c.chunk_no = ANY(%s)
                ORDER BY c.batch_id, c.seq_no
                """,
                (_as_uuid(thread_id), list(chunk_nos)),
            ).fetchall()


def _json_safe(value: Any) -> Any:
    """把 UUID/datetime 等转成可写入 JSONB 的结构。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
