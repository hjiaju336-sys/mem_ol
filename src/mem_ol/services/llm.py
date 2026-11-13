from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any, Sequence, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMService:
    """OpenAI 兼容 Chat Completions 封装。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout=self.settings.openai_timeout,
            max_retries=self.settings.openai_max_retries,
        )

    def _create(
        self,
        *,
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_tokens: int | None,
        timeout: float | None,
        response_format: dict[str, str] | None = None,
    ):
        """统一请求入口：429/5xx 退避重试，其余错误直接抛出。"""
        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if response_format is not None:
            kwargs["response_format"] = response_format

        for attempt in range(3):
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                if status not in _RETRYABLE_STATUSES or attempt == 2:
                    raise
                delay = 5 * (attempt + 1)
                logger.warning(
                    "LLM 请求限流，退避重试",
                    extra={
                        "event": "llm.request.backoff",
                        "attempt": attempt + 1,
                        "delay_s": delay,
                        "status": status,
                    },
                )
                time.sleep(delay)

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        started = time.perf_counter()
        response = self._create(
            messages=messages,
            temperature=(
                self.settings.openai_temperature
                if temperature is None
                else temperature
            ),
            max_tokens=max_tokens,
            timeout=timeout,
        )
        content = response.choices[0].message.content or ""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "LLM 回复完成",
            extra={
                "event": "llm.chat.done",
                "model": self.settings.openai_model,
                "message_count": len(messages),
                "reply_chars": len(content),
                "cost_ms": elapsed_ms,
            },
        )
        return content.strip()

    def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = 1600,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """要求模型输出 JSON；截断时提高上限，解析失败时修复/重试一次。"""
        current = list(messages)
        last_text = ""
        current_max_tokens = max_tokens
        for attempt in range(2):
            started = time.perf_counter()
            try:
                response = self._create(
                    messages=current,
                    temperature=temperature,
                    max_tokens=current_max_tokens,
                    timeout=timeout,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:  # noqa: BLE001
                # 仅当服务端不支持 response_format 时才降级重发；
                # 超时/网络错误不能重发一次，否则单次调用时间翻倍。
                status = getattr(exc, "status_code", None)
                if status not in (400, 404, 422):
                    raise
                logger.warning(
                    "JSON 模式不受支持，降级为普通请求",
                    extra={"event": "llm.json.fallback", "error": str(exc)},
                )
                response = self._create(
                    messages=current,
                    temperature=temperature,
                    max_tokens=current_max_tokens,
                    timeout=timeout,
                )

            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            last_text = (choice.message.content or "").strip()

            # 截断时不要直接解析半截 JSON，先提高输出上限重试。
            if finish_reason == "length" and attempt == 0:
                if current_max_tokens is not None:
                    current_max_tokens = min(current_max_tokens * 2, 8192)
                logger.warning(
                    "LLM JSON 输出被截断，重试",
                    extra={
                        "event": "llm.json.truncated",
                        "attempt": attempt + 1,
                        "max_tokens": current_max_tokens,
                        "response_chars": len(last_text),
                    },
                )
                current = current + [
                    {
                        "role": "user",
                        "content": (
                            "上一次输出被截断了。请精简内容，"
                            "只输出一个完整、可被 json.loads 解析的 JSON 对象；"
                            "不要输出 Markdown 代码块或任何解释。"
                        ),
                    }
                ]
                continue

            try:
                parsed = _extract_json(last_text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM JSON 解析失败",
                    extra={
                        "event": "llm.json.parse_failed",
                        "attempt": attempt + 1,
                        "finish_reason": finish_reason,
                        "response_chars": len(last_text),
                        "error": str(exc),
                        "preview": last_text[:300],
                    },
                )
                if attempt == 0:
                    current = current + [
                        {
                            "role": "user",
                            "content": (
                                "上一次输出不是合法 JSON。请只输出一个 JSON 对象，"
                                "不要 Markdown 代码块，不要解释；字符串内不要换行，"
                                "不要使用英文双引号，必要时用「」。"
                                f"错误：{exc}"
                            ),
                        }
                    ]
                    continue
                raise ValueError("LLM 未返回合法 JSON") from exc

            logger.info(
                "LLM JSON 回复完成",
                extra={
                    "event": "llm.json.done",
                    "model": self.settings.openai_model,
                    "attempt": attempt + 1,
                    "finish_reason": finish_reason,
                    "response_chars": len(last_text),
                    "cost_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return parsed
        raise ValueError("LLM 未返回合法 JSON")

    def chat_json_model(
        self,
        model_cls: type[ModelT],
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = 1600,
        timeout: float | None = None,
    ) -> ModelT:
        """JSON 输出 + 结构校验；校验失败时把错误回传模型重试一次。"""
        current = list(messages)
        for attempt in range(2):
            raw = self.chat_json(
                current,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            try:
                return model_cls.model_validate(raw)
            except ValidationError as exc:
                errors = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: "
                    f"{error['msg']}"
                    for error in exc.errors()[:5]
                )
                logger.warning(
                    "LLM JSON 结构校验失败",
                    extra={
                        "event": "llm.schema.failed",
                        "attempt": attempt + 1,
                        "model": model_cls.__name__,
                        "error": errors,
                    },
                )
                if attempt == 1:
                    raise
                current = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "上一次 JSON 未通过结构校验："
                            f"{errors}。请修正后只输出一个符合要求的 "
                            "JSON 对象，不要解释。"
                        ),
                    }
                ]
        raise ValueError("LLM 未返回符合结构的 JSON")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text:
        raise ValueError("LLM 返回空内容")

    start = text.find("{")
    end = text.rfind("}")
    candidate = text[start : end + 1] if start >= 0 and end > start else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        # 常见问题：截断、字符串内未转义引号/换行。先尝试修复。
        from json_repair import loads as repair_loads

        value = repair_loads(text)
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    return value


@lru_cache(maxsize=1)
def get_llm_service() -> LLMService:
    return LLMService()
