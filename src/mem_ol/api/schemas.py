from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from mem_ol.config import get_settings


class ThreadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ClientContext(BaseModel):
    """玩家侧内容：服务端不持久化，仅本轮使用。"""

    custom_prompt: str = ""
    pending_instructions: list[str] = Field(default_factory=list)
    lorebook: dict[str, Any] | None = None

    @field_validator("custom_prompt")
    @classmethod
    def cap_custom_prompt(cls, value: str) -> str:
        return str(value or "")[: get_settings().custom_prompt_max_chars]

    @field_validator("pending_instructions")
    @classmethod
    def trim_instructions(cls, value: list[str]) -> list[str]:
        settings = get_settings()
        return [
            str(item).strip()[: settings.pending_instruction_char_limit]
            for item in value
            if str(item).strip()
        ][: settings.pending_instruction_max]


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)
    checkpoint_id: str | None = None
    client_context: ClientContext | None = None


class LorebookPreviewRequest(BaseModel):
    lorebook: dict[str, Any]
    text: str = ""
    recent_messages: list[str] = Field(default_factory=list)


class WorldviewRequest(BaseModel):
    text: str = Field(min_length=1)
    source_name: str | None = None


class WorldImportRequest(BaseModel):
    text: str = Field(min_length=1)
    source_name: str | None = None
    append: bool = False
    doc_id: str | None = None
    char_start: int = 0
    char_end: int | None = None


class RollbackRequest(BaseModel):
    target_turn: int = Field(ge=0)
