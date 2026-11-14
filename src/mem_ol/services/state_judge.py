from __future__ import annotations

from typing import Any

from mem_ol.config import Settings, get_settings
from mem_ol.domain import StateJudgeResult
from mem_ol.log import get_logger
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.services.prompts import load_prompt, render_prompt
from mem_ol.services.state_render import format_resolution, format_state_card

logger = get_logger(__name__)


class StateJudge:
    """独立状态裁决：读正文与状态，产出 effects；失败不阻塞正文。"""

    def __init__(
        self,
        llm: LLMService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_service()

    def propose(
        self,
        *,
        user_action: str,
        reply: str,
        state_view: dict[str, Any] | None = None,
        resolution: dict[str, Any] | None = None,
        recent_context: str = "",
    ) -> dict[str, Any]:
        system, user_template = load_prompt("state_judge.md")
        user_prompt = render_prompt(
            user_template,
            user_action=user_action,
            gm_reply=reply,
            state_card=format_state_card(state_view or {})
            or "【当前状态（权威）】（无）",
            resolution_block=format_resolution(resolution or {})
            or "【本轮结算】无检定结算",
            recent_context=recent_context or "（无）",
        )
        try:
            result = self.llm.chat_json_model(
                StateJudgeResult,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=self.settings.state_judge_max_tokens,
                timeout=self.settings.dialogue_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "状态裁决失败，本轮不记录后果",
                extra={"event": "state.judge.failed", "error": str(exc)},
            )
            return {"effects": [], "appearances": []}
        return {
            "effects": [item.model_dump() for item in result.effects],
            "appearances": list(result.appearances),
        }
