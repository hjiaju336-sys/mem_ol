from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.domain import CommitResult
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.services.prompts import load_prompt, render_prompt

logger = get_logger(__name__)


def source_order_base(settings: Settings, turn_no: int | None) -> int:
    """对话顺序固定高于世界导入基线（batch_seq*1_000_000）。"""
    return settings.dialogue_order_base + int(turn_no or 0) * 1_000


class EntityCommitService:
    """API 3：判断实体存在性，抽取属性/关系变更，经 MCP 工具固化。"""

    def __init__(
        self,
        tools: EntityToolService,
        llm: LLMService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.tools = tools
        self.llm = llm or get_llm_service()
        self.settings = settings or get_settings()

    def run(
        self,
        thread_id: uuid.UUID | str,
        user_message: str,
        gm_reply: str,
        packet: dict[str, Any],
        *,
        turn_no: int | None = None,
        batch_id: str | None = None,
        occurred_at: datetime | str | None = None,
        message_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        tid = str(thread_id)
        trace_id = str(packet.get("trace_id") or uuid.uuid4())
        occurred = occurred_at or datetime.now(timezone.utc)

        with log_context(thread_id=tid):
            candidates = _compact_candidates(packet.get("entity_candidates", []))
            context_candidates = packet.get("context_candidates", [])
            system, user_template = load_prompt("entity_commit.md")
            user_prompt = render_prompt(
                user_template,
                user_message=user_message,
                gm_reply=gm_reply,
                hypothetical_entities_json=_json(
                    packet.get("hypothetical_entities", [])
                ),
                entity_candidates_json=_json(candidates),
                context_snippets=_context_snippets(context_candidates),
            )

            plan = self.llm.chat_json_model(
                CommitResult,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=6000,
                timeout=self.settings.dialogue_timeout,
            )
            # 对话轮次排在世界导入基线之后；世界导入按 batch_seq 占用低位。
            order_base = source_order_base(self.settings, turn_no)
            for decision in plan.decisions:
                for index, change in enumerate(decision.attribute_changes):
                    change.source_order = order_base + index
                for index, relation in enumerate(decision.relations):
                    relation.source_order = order_base + index
            if not plan.decisions:
                logger.info(
                    "API 3 无实体写入",
                    extra={
                        "event": "api3.commit.noop",
                        "trace_id": trace_id,
                        "turn_no": turn_no,
                    },
                )
                return {
                    "status": "noop",
                    "trace_id": trace_id,
                    "turn_summary": plan.turn_summary,
                    "decisions": [],
                }

            source_chunk_nos = _dedupe(
                [
                    str(item["chunk_no"])
                    for item in context_candidates
                    if item.get("chunk_no")
                ]
            )
            source_message_ids = _dedupe(
                [
                    str(message_id)
                    for item in context_candidates
                    for message_id in item.get("message_ids", [])
                ]
                + list(message_ids or [])
            )

            result = self.tools.entity_commit_plan(
                thread_id=tid,
                trace_id=trace_id,
                turn_no=turn_no,
                batch_id=batch_id,
                occurred_at=occurred.isoformat()
                if isinstance(occurred, datetime)
                else str(occurred),
                decisions=[decision.model_dump() for decision in plan.decisions],
                source_chunk_nos=source_chunk_nos,
                source_message_ids=source_message_ids,
            )

            logger.info(
                "API 3 实体固化完成",
                extra={
                    "event": "api3.commit.done",
                    "trace_id": trace_id,
                    "turn_no": turn_no,
                    "decision_count": len(plan.decisions),
                    "relation_count": result.get("relation_count", 0),
                    "index_error_count": len(result.get("index_errors", [])),
                },
            )
            return {
                "status": "done",
                "turn_summary": plan.turn_summary,
                **result,
            }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _compact_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for candidate in candidates:
        compact.append(
            {
                "entity_id": candidate.get("entity_id"),
                "canonical_name": candidate.get("canonical_name"),
                "entity_type": candidate.get("entity_type"),
                "aliases": candidate.get("aliases", []),
                "summary": candidate.get("summary", ""),
                "attributes": [
                    {
                        "key": attr.get("key"),
                        "value": attr.get("value"),
                        "occurred_at": attr.get("occurred_at"),
                        "evidence": attr.get("evidence"),
                    }
                    for attr in candidate.get("attributes", [])[:4]
                ],
                "relations": [
                    {
                        "source_id": rel.get("source_id"),
                        "source_name": rel.get("source_name"),
                        "target_id": rel.get("target_id"),
                        "target_name": rel.get("target_name"),
                        "relation_label": rel.get("relation_label"),
                    }
                    for rel in candidate.get("relations", [])[:4]
                ],
                "rrf_score": candidate.get("rrf_score"),
            }
        )
    return compact


def _context_snippets(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for candidate in candidates[:4]:
        text = candidate.get("parent_content") or candidate.get("content") or ""
        text = str(text).strip()
        if len(text) > 600:
            text = text[:600] + "……"
        chunk_no = candidate.get("chunk_no")
        lines.append(f"[{chunk_no}]\n{text}")
    return "\n\n".join(lines) or "（无）"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
