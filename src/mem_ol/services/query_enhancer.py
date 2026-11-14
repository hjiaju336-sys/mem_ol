from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass
class QueryAnalysis:
    original_query: str
    rewritten_query: str


class QueryEnhancer:
    """查询增强/意图识别 fallback。

    正常路径由 API 2 的 LLM 改写查询；
    本类只在 LLM 失败时返回原文，保证检索链路不中断。
    """

    def analyze(
        self,
        user_message: str,
        recent_messages: Sequence[dict[str, Any]] = (),
    ) -> QueryAnalysis:
        return QueryAnalysis(
            original_query=user_message,
            rewritten_query=user_message,
        )
