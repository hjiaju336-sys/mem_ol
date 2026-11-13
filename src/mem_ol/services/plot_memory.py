from __future__ import annotations

import uuid
from typing import Any, Sequence

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.services.embedding import EmbeddingService
from mem_ol.services.llm import LLMService
from mem_ol.services.prompts import load_prompt, render_prompt
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class PlotMemoryService:
    """L0 单轮摘要 → L1 章节摘要 → L2 全局梗概的维护。"""

    def __init__(
        self,
        store: PostgresStore,
        embedder: EmbeddingService,
        llm: LLMService,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.settings = settings or get_settings()

    def maintain(
        self, thread_id: uuid.UUID | str, current_turn: int
    ) -> dict[str, int]:
        tid = str(thread_id)
        with log_context(thread_id=tid):
            archived = self.archive_due(tid, current_turn)
            new_arcs = self.build_arcs(tid)
            updated = self.update_global(tid, new_arcs)
        return {
            "archived_turns": archived,
            "new_arcs": len(new_arcs),
            "global_updated": int(updated),
        }

    def archive_due(
        self, thread_id: uuid.UUID | str, current_turn: int
    ) -> int:
        cutoff = int(current_turn) - self.settings.dialogue_history_turns
        if cutoff <= 0:
            return 0
        nodes = [
            node
            for node in self.store.list_active_turn_summaries(
                thread_id, cutoff
            )
            if int(node.get("turn_no") or 0) <= cutoff
        ]
        if not nodes:
            return 0
        vectors = self.embedder.embed(
            [str(node["summary"]) for node in nodes]
        )
        for node, vector in zip(nodes, vectors):
            node["embedding"] = vector
        archived = self.store.mark_plot_nodes_archived(nodes)
        logger.info(
            "单轮摘要归档",
            extra={"event": "plot.archive.done", "count": archived},
        )
        return archived

    def build_arcs(self, thread_id: uuid.UUID | str) -> list[dict[str, Any]]:
        rows = self.store.list_turn_summaries(thread_id)
        by_turn = {int(row["turn_no"]): row for row in rows}
        if not by_turn:
            return []
        covered = {
            (int(arc["turn_start"]), int(arc["turn_end"]))
            for arc in self.store.list_arcs(
                thread_id, max_turn_end=10**9, limit=1000
            )
        }
        size = max(1, int(self.settings.plot_arc_size))
        max_turn = max(by_turn)
        new_arcs: list[dict[str, Any]] = []
        for start in range(1, max_turn + 1, size):
            end = start + size - 1
            if (start, end) in covered:
                continue
            if not all(turn in by_turn for turn in range(start, end + 1)):
                continue
            summaries = [
                str(by_turn[turn]["summary"])
                for turn in range(start, end + 1)
            ]
            summary = self._merge_arc(summaries)
            vector = self.embedder.embed([summary])[0]
            self.store.upsert_arc_summary(
                thread_id, start, end, summary, vector
            )
            new_arcs.append(
                {
                    "turn_start": start,
                    "turn_end": end,
                    "summary": summary,
                }
            )
            logger.info(
                "剧情章节摘要生成",
                extra={
                    "event": "plot.arc.done",
                    "turn_start": start,
                    "turn_end": end,
                },
            )
        return new_arcs

    def update_global(
        self,
        thread_id: uuid.UUID | str,
        new_arcs: Sequence[dict[str, Any]],
    ) -> bool:
        if not new_arcs:
            if self.store.get_global_plot(thread_id) is not None:
                return False
            existing = self.store.list_arcs(
                thread_id, max_turn_end=10**9, limit=1000
            )
            if not existing:
                return False
            new_arcs = [
                {
                    "turn_start": arc["turn_start"],
                    "turn_end": arc["turn_end"],
                    "summary": arc["summary"],
                }
                for arc in existing
            ]
        current = self.store.get_global_plot(thread_id)
        old_summary = str(current["summary"]) if current else ""
        new_text = "\n".join(
            f"[第{arc['turn_start']}—{arc['turn_end']}轮] {arc['summary']}"
            for arc in new_arcs
        )
        if not old_summary and len(new_arcs) == 1:
            summary = str(new_arcs[0]["summary"])
        else:
            summary = self._merge_global(old_summary, new_text)
        self.store.upsert_global_plot(thread_id, summary)
        logger.info(
            "全局主线梗概更新",
            extra={
                "event": "plot.global.done",
                "arc_count": len(new_arcs),
            },
        )
        return True

    def _merge_arc(self, summaries: Sequence[str]) -> str:
        system, user_template = load_prompt("plot_arc.md")
        user_prompt = render_prompt(
            user_template,
            turn_summaries="\n".join(summaries),
        )
        try:
            reply = self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=300,
                timeout=self.settings.dialogue_timeout,
            )
            return reply.strip() or "；".join(summaries)[:400]
        except Exception:  # noqa: BLE001
            logger.exception(
                "章节摘要生成失败，使用拼接兜底",
                extra={"event": "plot.arc.failed"},
            )
            return "；".join(summaries)[:400]

    def _merge_global(self, old_summary: str, new_text: str) -> str:
        system, user_template = load_prompt("plot_global.md")
        user_prompt = render_prompt(
            user_template,
            old_summary=old_summary or "（无）",
            new_arcs=new_text,
        )
        try:
            reply = self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=400,
                timeout=self.settings.dialogue_timeout,
            )
            return reply.strip() or f"{old_summary}\n{new_text}"[:600]
        except Exception:  # noqa: BLE001
            logger.exception(
                "全局梗概生成失败，使用拼接兜底",
                extra={"event": "plot.global.failed"},
            )
            return f"{old_summary}\n{new_text}"[:600]
