from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Sequence

from neo4j import GraphDatabase

from mem_ol.log import get_logger

logger = get_logger(__name__)

ENTITY_TYPES = (
    "character",
    "location",
    "item",
    "faction",
    "event",
    "concept",
    "other",
)

_FULLTEXT_INDEX = "entity_name_fulltext"
_REL_FULLTEXT_INDEX = "relation_label_fulltext"
_FULLTEXT_CALL = "db.index.fulltext.queryNodes"
_REL_FULLTEXT_CALL = "db.index.fulltext.queryRelationships"


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _escape_lucene(term: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", term)


class Neo4jStore:
    """Neo4j 实体图访问层。实体、属性变更、关系以这里为准。"""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def init_schema(self) -> None:
        statements = [
            """
            CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
            FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE
            """,
            """
            CREATE CONSTRAINT attribute_change_id_unique IF NOT EXISTS
            FOR (a:AttributeChange) REQUIRE a.change_id IS UNIQUE
            """,
            """
            CREATE INDEX entity_thread IF NOT EXISTS
            FOR (e:Entity) ON (e.thread_id)
            """,
            """
            CREATE INDEX attribute_thread_time IF NOT EXISTS
            FOR (a:AttributeChange) ON (a.thread_id, a.occurred_at)
            """,
            """
            CREATE INDEX rel_thread IF NOT EXISTS
            FOR ()-[r:REL]-() ON (r.thread_id)
            """,
            """
            CREATE FULLTEXT INDEX entity_name_fulltext IF NOT EXISTS
            FOR (e:Entity) ON EACH [e.canonical_name, e.aliases]
            OPTIONS { indexConfig: { `fulltext.analyzer`: 'cjk' } }
            """,
            """
            CREATE FULLTEXT INDEX relation_label_fulltext IF NOT EXISTS
            FOR ()-[r:REL]-() ON EACH [r.relation_label]
            OPTIONS { indexConfig: { `fulltext.analyzer`: 'cjk' } }
            """,
        ]
        for statement in statements:
            try:
                with self.driver.session() as session:
                    session.run(statement).consume()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Neo4j schema 语句失败",
                    extra={
                        "event": "neo4j.schema.failed",
                        "error": str(exc),
                        "statement": statement.strip().splitlines()[0],
                    },
                )

    # ---------- read ----------

    def search_entities_keyword(
        self,
        thread_id: uuid.UUID | str,
        keywords: Sequence[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        terms = [str(term).strip() for term in keywords if str(term).strip()]
        if not terms:
            return []
        tid = str(thread_id)

        fulltext_query = " OR ".join(_escape_lucene(term) for term in terms)
        cypher = f"""
        CALL {_FULLTEXT_CALL}($index, $search_query) YIELD node, score
        WHERE node.thread_id = $thread_id
        RETURN node.entity_id AS entity_id,
               node.canonical_name AS canonical_name,
               node.entity_type AS entity_type,
               coalesce(node.aliases, []) AS aliases,
               CASE WHEN any(k IN $keywords WHERE node.canonical_name CONTAINS k)
                    THEN 'canonical_name' ELSE 'alias' END AS matched_field,
               score
        ORDER BY score DESC
        LIMIT $limit
        """
        try:
            with self.driver.session() as session:
                rows = session.run(
                    cypher,
                    index=_FULLTEXT_INDEX,
                    search_query=fulltext_query,
                    thread_id=tid,
                    keywords=terms,
                    limit=int(limit),
                ).data()
            if rows:
                return _jsonable(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Neo4j 全文检索失败，降级 CONTAINS",
                extra={
                    "event": "neo4j.fulltext.fallback",
                    "thread_id": tid,
                    "error": str(exc),
                },
            )

        fallback = """
        MATCH (e:Entity {thread_id: $thread_id})
        WHERE any(k IN $keywords
                  WHERE e.canonical_name CONTAINS k
                     OR any(a IN coalesce(e.aliases, []) WHERE a CONTAINS k))
        RETURN e.entity_id AS entity_id,
               e.canonical_name AS canonical_name,
               e.entity_type AS entity_type,
               coalesce(e.aliases, []) AS aliases,
               CASE WHEN any(k IN $keywords WHERE e.canonical_name CONTAINS k)
                    THEN 'canonical_name' ELSE 'alias' END AS matched_field,
               0.5 AS score
        LIMIT $limit
        """
        with self.driver.session() as session:
            rows = session.run(
                fallback,
                thread_id=tid,
                keywords=terms,
                limit=int(limit),
            ).data()
        return _jsonable(rows)

    def search_relations_keyword(
        self,
        thread_id: uuid.UUID | str,
        keywords: Sequence[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """按关系标签关键词查边；先走关系全文索引，失败退化为 CONTAINS。"""
        terms = [str(term).strip() for term in keywords if str(term).strip()]
        if not terms:
            return []
        tid = str(thread_id)
        fulltext_query = " OR ".join(_escape_lucene(term) for term in terms)
        cypher = f"""
        CALL {_REL_FULLTEXT_CALL}($index, $search_query)
        YIELD relationship, score
        WHERE startNode(relationship).thread_id = $thread_id
          AND endNode(relationship).thread_id = $thread_id
        RETURN startNode(relationship).entity_id AS source_id,
               startNode(relationship).canonical_name AS source_name,
               coalesce(relationship.relation_label, '') AS relation_label,
               endNode(relationship).entity_id AS target_id,
               endNode(relationship).canonical_name AS target_name,
               score
        ORDER BY score DESC
        LIMIT $limit
        """
        try:
            with self.driver.session() as session:
                rows = session.run(
                    cypher,
                    index=_REL_FULLTEXT_INDEX,
                    search_query=fulltext_query,
                    thread_id=tid,
                    limit=int(limit),
                ).data()
            if rows:
                return _jsonable(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "关系全文检索失败，降级 CONTAINS",
                extra={
                    "event": "neo4j.rel_fulltext.fallback",
                    "thread_id": tid,
                    "error": str(exc),
                },
            )

        fallback = """
        MATCH (s:Entity {thread_id: $thread_id})-[r:REL]->(t:Entity {thread_id: $thread_id})
        WHERE any(k IN $keywords
                  WHERE r.relation_label CONTAINS k)
        RETURN s.entity_id AS source_id,
               s.canonical_name AS source_name,
               r.relation_label AS relation_label,
               t.entity_id AS target_id,
               t.canonical_name AS target_name,
               0.5 AS score
        LIMIT $limit
        """
        with self.driver.session() as session:
            rows = session.run(
                fallback,
                thread_id=tid,
                keywords=terms,
                limit=int(limit),
            ).data()
        return _jsonable(rows)

    def list_characters_created_at(
        self, thread_id: uuid.UUID | str, turn_no: int
    ) -> list[dict[str, Any]]:
        """某轮 API 3 新建的角色实体；用于把状态角色补进 game_states。"""
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                WHERE e.entity_type = 'character'
                  AND coalesce(e.created_turn_no, 0) = $turn_no
                RETURN e.entity_id AS entity_id,
                       e.canonical_name AS canonical_name,
                       e.entity_type AS entity_type
                LIMIT 50
                """,
                thread_id=str(thread_id),
                turn_no=int(turn_no),
            ).data()
        return _jsonable(rows)

    def find_entity_by_name(
        self, thread_id: uuid.UUID | str, name: str
    ) -> dict[str, Any] | None:
        """按规范名或别名精确匹配；只用于把状态挂到实体，不做模糊绑定。"""
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                WHERE e.canonical_name = $name
                   OR $name IN coalesce(e.aliases, [])
                RETURN e.entity_id AS entity_id,
                       e.canonical_name AS canonical_name,
                       e.entity_type AS entity_type
                LIMIT 1
                """,
                thread_id=str(thread_id),
                name=cleaned,
            ).single()
        return _jsonable(dict(record)) if record else None

    def get_entity(
        self,
        thread_id: uuid.UUID | str,
        entity_id: uuid.UUID | str,
        *,
        include_attributes: bool = True,
        include_relations: bool = True,
    ) -> dict[str, Any] | None:
        tid = str(thread_id)
        eid = str(entity_id)
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
                RETURN e
                """,
                entity_id=eid,
                thread_id=tid,
            ).single()
            if record is None:
                return None

            entity = dict(record["e"])
            attributes: list[dict[str, Any]] = []
            if include_attributes:
                attributes = session.run(
                    """
                    MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
                          -[:HAS_ATTRIBUTE]->(a:AttributeChange)
                    WITH a.key AS key, a
                    ORDER BY coalesce(a.source_order, 0) DESC,
                             a.occurred_at DESC
                    WITH key, collect(a)[0] AS latest
                    RETURN key,
                           latest.value AS value,
                           latest.occurred_at AS occurred_at,
                           latest.evidence AS evidence,
                           latest.source_order AS source_order,
                           latest.batch_id AS batch_id,
                           latest.trace_id AS trace_id,
                           latest.turn_no AS turn_no,
                           latest.source_chunk_nos AS source_chunk_nos,
                           latest.source_message_ids AS source_message_ids
                    ORDER BY key
                    """,
                    entity_id=eid,
                    thread_id=tid,
                ).data()

            relations: list[dict[str, Any]] = []
            if include_relations:
                relations = session.run(
                    """
                    MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
                          -[r:REL]-(n:Entity)
                    WHERE n.thread_id = $thread_id
                    RETURN startNode(r).entity_id AS source_id,
                           startNode(r).canonical_name AS source_name,
                           endNode(r).entity_id AS target_id,
                           endNode(r).canonical_name AS target_name,
                           r.rel_id AS rel_id,
                           r.relation_label AS relation_label,
                           r.source_order AS source_order,
                           r.batch_id AS batch_id,
                           r.trace_id AS trace_id,
                           r.turn_no AS turn_no,
                           r.source_chunk_nos AS source_chunk_nos,
                           r.source_message_ids AS source_message_ids
                    """,
                    entity_id=eid,
                    thread_id=tid,
                ).data()

        return _jsonable(
            {
                "entity": entity,
                "attributes": attributes,
                "relations": relations,
            }
        )

    def get_neighborhood(
        self,
        thread_id: uuid.UUID | str,
        entity_ids: Sequence[str],
        depth: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if depth not in (1, 2):
            raise ValueError("depth 只支持 1 或 2")
        ids = [str(item) for item in entity_ids]
        if not ids:
            return []
        cypher = f"""
        UNWIND $entity_ids AS eid
        MATCH path = (e:Entity {{entity_id: eid, thread_id: $thread_id}})
                     -[:REL*1..{depth}]-(n:Entity)
        WHERE all(x IN nodes(path) WHERE x.thread_id = $thread_id)
        RETURN [node IN nodes(path) | {{
                    entity_id: node.entity_id,
                    name: node.canonical_name,
                    type: node.entity_type
                }}] AS nodes,
               [rel IN relationships(path) | {{
                    rel_id: rel.rel_id,
                    relation_label: rel.relation_label,
                    start_id: startNode(rel).entity_id,
                    end_id: endNode(rel).entity_id
                }}] AS edges
        LIMIT $limit
        """
        with self.driver.session() as session:
            rows = session.run(
                cypher,
                entity_ids=ids,
                thread_id=str(thread_id),
                limit=int(limit),
            ).data()
        return _jsonable(rows)

    def entities_exist(
        self, thread_id: uuid.UUID | str, entity_ids: Sequence[str]
    ) -> set[str]:
        ids = [str(item) for item in entity_ids]
        if not ids:
            return set()
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                WHERE e.entity_id IN $entity_ids
                RETURN e.entity_id AS entity_id
                """,
                thread_id=str(thread_id),
                entity_ids=ids,
            ).data()
        return {row["entity_id"] for row in rows}

    # ---------- rollback ----------

    def rollback_after_turn(
        self, thread_id: uuid.UUID | str, target_turn: int
    ) -> dict[str, int]:
        """删除目标轮次之后的属性变更、关系和新建实体。"""
        tid = str(thread_id)

        def _write(tx):
            attribute_count = tx.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                      -[r:HAS_ATTRIBUTE]->(a:AttributeChange)
                WHERE a.turn_no > $target_turn
                WITH collect(a) AS nodes
                FOREACH (node IN nodes | DETACH DELETE node)
                RETURN size(nodes) AS deleted
                """,
                thread_id=tid,
                target_turn=int(target_turn),
            ).single()["deleted"]

            relation_count = tx.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})-[r:REL]->(n:Entity)
                WHERE r.turn_no > $target_turn
                WITH collect(r) AS rels
                FOREACH (rel IN rels | DELETE rel)
                RETURN size(rels) AS deleted
                """,
                thread_id=tid,
                target_turn=int(target_turn),
            ).single()["deleted"]

            entity_count = tx.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                WHERE e.created_turn_no > $target_turn
                WITH collect(e) AS nodes
                FOREACH (node IN nodes | DETACH DELETE node)
                RETURN size(nodes) AS deleted
                """,
                thread_id=tid,
                target_turn=int(target_turn),
            ).single()["deleted"]

            tx.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                WHERE e.last_turn_no > $target_turn
                SET e.last_turn_no = $target_turn
                """,
                thread_id=tid,
                target_turn=int(target_turn),
            ).consume()

            return {
                "attributes": int(attribute_count),
                "relations": int(relation_count),
                "entities": int(entity_count),
            }

        with self.driver.session() as session:
            return session.execute_write(_write)

    # ---------- write ----------

    def commit_plan(
        self,
        *,
        thread_id: uuid.UUID | str,
        trace_id: uuid.UUID | str,
        turn_no: int | None,
        batch_id: uuid.UUID | str | None,
        occurred_at: datetime,
        decisions: Sequence[dict[str, Any]],
        relation_specs: Sequence[dict[str, Any]],
        source_chunk_nos: Sequence[str],
        source_message_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        """在一个 Neo4j 事务里写实体、属性变更与关系。

        decisions 已由工具层完成 ID 分配、别名对比与 target_ref 解析；
        relation_specs 里的 source_id/target_id 都必须是已存在的实体。
        """
        tid = str(thread_id)
        trid = str(trace_id)
        bid = str(batch_id) if batch_id else ""
        chunk_nos = list(source_chunk_nos)
        message_ids = list(source_message_ids)

        def _write(tx):
            results: list[dict[str, Any]] = []

            for decision in decisions:
                action = decision["action"]
                if action == "skip":
                    results.append(
                        {
                            "surface_form": decision.get("surface_form"),
                            "action": "skip",
                            "entity_id": None,
                        }
                    )
                    continue

                aliases = list(decision.get("aliases", []))
                new_aliases = list(decision.get("new_aliases", []))
                record = tx.run(
                    """
                    MERGE (e:Entity {entity_id: $entity_id})
                    ON CREATE SET e.thread_id = $thread_id,
                                  e.user_id = $user_id,
                                  e.canonical_name = $canonical_name,
                                  e.normalized_name = $normalized_name,
                                  e.entity_type = $entity_type,
                                  e.aliases = $aliases,
                                  e.summary = $summary,
                                  e.status = 'active',
                                  e.first_seen_at = $occurred_at,
                                  e.last_seen_at = $occurred_at,
                                  e.last_batch_id = $batch_id,
                                  e.created_turn_no = $created_turn_no,
                                  e.created_batch_id = $created_batch_id,
                                  e.last_turn_no = $created_turn_no
                    ON MATCH SET e.last_seen_at = $occurred_at,
                                 e.last_batch_id = $batch_id,
                                 e.last_turn_no = $last_turn_no,
                                 e.aliases = reduce(
                                     acc = coalesce(e.aliases, []),
                                     x IN $new_aliases |
                                     CASE WHEN x IN acc THEN acc ELSE acc + x END
                                 ),
                                 e.summary = CASE WHEN $summary = ''
                                                  THEN coalesce(e.summary, '')
                                                  ELSE $summary END
                    WITH e
                    WHERE e.thread_id = $thread_id
                    RETURN e.entity_id AS entity_id
                    """,
                    entity_id=str(decision["entity_id"]),
                    thread_id=tid,
                    user_id=decision.get("user_id"),
                    canonical_name=decision["canonical_name"],
                    normalized_name=decision["normalized_name"],
                    entity_type=decision["entity_type"],
                    aliases=aliases,
                    new_aliases=new_aliases,
                    summary=decision.get("summary") or "",
                    occurred_at=occurred_at,
                    batch_id=bid,
                    created_turn_no=int(decision.get("created_turn_no") or 0),
                    created_batch_id=(
                        str(decision["created_batch_id"])
                        if decision.get("created_batch_id")
                        else None
                    ),
                    last_turn_no=int(decision.get("last_turn_no") or 0),
                ).single()
                if record is None:
                    raise ValueError(
                        f"实体写入失败或线程不匹配: {decision['entity_id']}"
                    )

                attribute_changes = list(decision.get("attribute_changes", []))
                if attribute_changes:
                    tx.run(
                        """
                        MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
                        UNWIND $attribute_changes AS c
                        MERGE (a:AttributeChange {change_id: c.change_id})
                        ON CREATE SET a.thread_id = $thread_id,
                                      a.key = c.key,
                                      a.value = c.value,
                                      a.occurred_at = $occurred_at,
                                      a.batch_id = $batch_id,
                                      a.trace_id = $trace_id,
                                      a.turn_no = $turn_no,
                                      a.source_chunk_nos = $source_chunk_nos,
                                      a.source_message_ids = $source_message_ids,
                                      a.evidence = c.evidence,
                                      a.source_order = coalesce(c.source_order, 0)
                        MERGE (e)-[:HAS_ATTRIBUTE]->(a)
                        """,
                        entity_id=str(decision["entity_id"]),
                        thread_id=tid,
                        attribute_changes=attribute_changes,
                        occurred_at=occurred_at,
                        batch_id=bid,
                        trace_id=trid,
                        turn_no=turn_no,
                        source_chunk_nos=chunk_nos,
                        source_message_ids=message_ids,
                    ).consume()

                results.append(
                    {
                        "surface_form": decision.get("surface_form"),
                        "action": action,
                        "entity_id": str(decision["entity_id"]),
                        "attribute_changes": len(attribute_changes),
                    }
                )

            relation_count = 0
            if relation_specs:
                rows = tx.run(
                    """
                    UNWIND $relations AS rel
                    MATCH (s:Entity {entity_id: rel.source_id, thread_id: $thread_id})
                    MATCH (t:Entity {entity_id: rel.target_id, thread_id: $thread_id})
                    MERGE (s)-[r:REL {rel_id: rel.rel_id}]->(t)
                    ON CREATE SET r.relation_label = rel.relation_label,
                                  r.thread_id = $thread_id,
                                  r.batch_id = $batch_id,
                                  r.occurred_at = $occurred_at,
                                  r.trace_id = $trace_id,
                                  r.turn_no = $turn_no,
                                  r.source_chunk_nos = $source_chunk_nos,
                                  r.source_message_ids = $source_message_ids,
                                  r.source_order = coalesce(rel.source_order, 0)
                    RETURN count(r) AS relation_count
                    """,
                    relations=list(relation_specs),
                    thread_id=tid,
                    batch_id=bid,
                    occurred_at=occurred_at,
                    trace_id=trid,
                    turn_no=turn_no,
                    source_chunk_nos=chunk_nos,
                    source_message_ids=message_ids,
                ).single()
                relation_count = int(rows["relation_count"]) if rows else 0

            return {
                "decisions": results,
                "relation_count": relation_count,
            }

        with self.driver.session() as session:
            return session.execute_write(_write)

    def list_entities(self, thread_id: uuid.UUID | str) -> list[dict[str, Any]]:
        """线程内全部实体的轻量视图，供去重维护使用。"""
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (e:Entity {thread_id: $thread_id})
                RETURN e.entity_id AS entity_id,
                       e.canonical_name AS canonical_name,
                       e.entity_type AS entity_type,
                       coalesce(e.aliases, []) AS aliases,
                       coalesce(e.summary, '') AS summary,
                       coalesce(e.created_turn_no, 0) AS created_turn_no
                ORDER BY created_turn_no, canonical_name
                """,
                thread_id=str(thread_id),
            ).data()
        return _jsonable(rows)

    def merge_entities(
        self,
        thread_id: uuid.UUID | str,
        survivor_id: uuid.UUID | str,
        duplicate_ids: Sequence[uuid.UUID | str],
    ) -> dict[str, Any]:
        """把重复实体并入 survivor：别名、属性、关系重新挂接后删除重复节点。"""
        tid = str(thread_id)
        sid = str(survivor_id)
        aliases: list[str] = []
        merged = 0
        moved_attributes = 0
        moved_relations = 0

        def _add_names(name: Any, alias_values: Sequence[Any]) -> None:
            for value in [name, *alias_values]:
                text = str(value or "").strip()
                if text and text not in aliases:
                    aliases.append(text)

        with self.driver.session() as session:
            record = session.run(
                "MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id}) "
                "RETURN e",
                entity_id=sid,
                thread_id=tid,
            ).single()
            if record is None:
                raise ValueError(f"survivor 不存在: {survivor_id}")
            survivor = dict(record["e"])
            _add_names(
                survivor.get("canonical_name"), survivor.get("aliases") or []
            )

            for dup_value in duplicate_ids:
                dup_id = str(dup_value)
                record = session.run(
                    "MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id}) "
                    "RETURN e",
                    entity_id=dup_id,
                    thread_id=tid,
                ).single()
                if record is None:
                    continue
                dup = dict(record["e"])
                _add_names(dup.get("canonical_name"), dup.get("aliases") or [])
                merged += 1

                moved_attributes += int(
                    session.run(
                        """
                        MATCH (d:Entity {entity_id: $dup_id, thread_id: $thread_id})
                              -[r:HAS_ATTRIBUTE]->(a:AttributeChange)
                        MATCH (s:Entity {entity_id: $survivor_id, thread_id: $thread_id})
                        MERGE (s)-[:HAS_ATTRIBUTE]->(a)
                        DELETE r
                        RETURN count(a) AS moved
                        """,
                        dup_id=dup_id,
                        survivor_id=sid,
                        thread_id=tid,
                    ).single()["moved"]
                )

                for direction in ("out", "in"):
                    if direction == "out":
                        query = """
                        MATCH (d:Entity {entity_id: $dup_id, thread_id: $thread_id})
                              -[r:REL]->(t:Entity {thread_id: $thread_id})
                        RETURN t.entity_id AS other_id,
                               r.relation_label AS relation_label,
                               coalesce(r.source_order, 0) AS source_order
                        """
                    else:
                        query = """
                        MATCH (t:Entity {thread_id: $thread_id})
                              -[r:REL]->(d:Entity {entity_id: $dup_id, thread_id: $thread_id})
                        RETURN t.entity_id AS other_id,
                               r.relation_label AS relation_label,
                               coalesce(r.source_order, 0) AS source_order
                        """
                    edges = session.run(
                        query, dup_id=dup_id, thread_id=tid
                    ).data()
                    for edge in edges:
                        other_id = str(edge["other_id"])
                        if other_id == sid:
                            continue
                        label = str(edge["relation_label"] or "未明确")
                        source, target = (
                            (sid, other_id)
                            if direction == "out"
                            else (other_id, sid)
                        )
                        rel_id = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"{tid}:rel:{source}:{target}:{label}",
                            )
                        )
                        session.run(
                            """
                            MATCH (s:Entity {entity_id: $source_id, thread_id: $thread_id})
                            MATCH (t:Entity {entity_id: $target_id, thread_id: $thread_id})
                            MERGE (s)-[r:REL {rel_id: $rel_id}]->(t)
                            ON CREATE SET r.relation_label = $label,
                                          r.thread_id = $thread_id,
                                          r.source_order = $source_order
                            """,
                            source_id=source,
                            target_id=target,
                            rel_id=rel_id,
                            label=label,
                            source_order=int(edge.get("source_order") or 0),
                            thread_id=tid,
                        ).consume()
                        moved_relations += 1

                session.run(
                    "MATCH (d:Entity {entity_id: $dup_id, thread_id: $thread_id}) "
                    "DETACH DELETE d",
                    dup_id=dup_id,
                    thread_id=tid,
                ).consume()

            session.run(
                "MATCH (e:Entity {entity_id: $survivor_id, thread_id: $thread_id}) "
                "SET e.aliases = $aliases",
                survivor_id=sid,
                thread_id=tid,
                aliases=aliases,
            ).consume()

        return {
            "survivor_id": sid,
            "aliases": aliases,
            "merged_entities": merged,
            "moved_attributes": moved_attributes,
            "moved_relations": moved_relations,
        }
