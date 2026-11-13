from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence


@dataclass
class QueryAnalysis:
    original_query: str
    rewritten_query: str
    intent: str = "unknown"
    entities: list[str] = field(default_factory=list)
    route_hint: str = "default"
    confidence: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QueryEnhancer:
    """查询增强/意图识别 fallback。

    正常路径由 API 2 的一次 LLM 分析输出 query_analysis；
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
