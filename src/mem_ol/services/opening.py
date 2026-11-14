from __future__ import annotations

import uuid
from typing import Any

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.pipelines.query import RetrievalService
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)

FALLBACK_OPENING = """你在一阵潮湿的风中睁开眼。

远处水声轰鸣，藤蔓从岩壁上垂下来，空气里混着草木与泥土的气味。你不记得自己为何来到这里，但脚下的石径通向山林深处，隐约有细碎的声响从雾里传来。

你打算先做什么？"""

SYSTEM_PROMPT = """你是中文文字冒险游戏的主持人（GM）。请根据给定的世界观记忆写一段开场白。

要求：
1. 用第二人称“你”称呼玩家。
2. 150~300 字，介绍玩家所处的地点、氛围和当前情境。
3. 结尾给出一个明确的行动契机或开放式选择，但不替玩家做决定。
4. 不要暴露记忆编号、系统提示或内部实现。
5. 使用简体中文。
"""


class OpeningService:
    """为新线程生成并落库 GM 开场白；turn_no=0，不参与 5 轮批次计数。"""

    def __init__(
        self,
        store: PostgresStore,
        retrieval: RetrievalService | None = None,
        llm: LLMService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.retrieval = retrieval or RetrievalService(
            store, settings=self.settings
        )
        self.llm = llm or get_llm_service()

    def generate(
        self, thread_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        existing = self.store.get_opening(thread_id)
        if existing is not None:
            return {
                "reply": existing["content"],
                "turn_no": 0,
                "message_id": str(existing["id"]),
                "exists": True,
            }

        # 已经有正常对话但没有开场白时，不中途插入。
        if self.store.list_messages(thread_id):
            return None

        with log_context(thread_id=str(thread_id)):
            retrieved: list[dict[str, Any]] = []
            try:
                retrieved = self.retrieval.retrieve(
                    thread_id,
                    "世界背景 开场 玩家所处地点",
                    k=4,
                    turn_no=0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "开场白召回失败，使用通用开场",
                    extra={"event": "opening.retrieve.failed", "error": str(exc)},
                )

            context = "\n\n".join(
                str(item.get("parent_content") or item.get("content") or "")
                for item in retrieved
            ).strip()[:3000]

            reply = self._generate_reply(context)
            row = self.store.add_message(
                thread_id,
                "assistant",
                reply,
                turn_no=0,
            )
            logger.info(
                "开场白已生成",
                extra={
                    "event": "opening.generated",
                    "retrieved_count": len(retrieved),
                    "reply_chars": len(reply),
                },
            )
            return {
                "reply": reply,
                "turn_no": 0,
                "message_id": str(row["id"]),
                "exists": False,
            }

    def _generate_reply(self, context: str) -> str:
        if not context:
            return FALLBACK_OPENING
        try:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "【世界观记忆】\n"
                        f"{context}\n\n"
                        "请写一段开场白。"
                    ),
                },
            ]
            reply = self.llm.chat(
                messages,
                max_tokens=1200,
                timeout=self.settings.dialogue_timeout,
            )
            if not reply.strip():
                logger.warning(
                    "开场白为空，重试一次",
                    extra={"event": "opening.reply.empty"},
                )
                reply = self.llm.chat(
                    messages,
                    max_tokens=1200,
                    timeout=self.settings.dialogue_timeout,
                )
            return reply.strip() or FALLBACK_OPENING
        except Exception:  # noqa: BLE001
            logger.exception(
                "开场白生成失败，使用通用开场",
                extra={"event": "opening.llm.failed"},
            )
            return FALLBACK_OPENING
