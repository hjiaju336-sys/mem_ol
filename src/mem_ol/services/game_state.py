from __future__ import annotations

import uuid
from typing import Any

from mem_ol.services.numeric_engine import ensure_state, state_view
from mem_ol.storage.postgres import PostgresStore


class GameStateService:
    """游戏数值状态的读写入口；状态本体在 PostgreSQL JSONB。"""

    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def load(self, thread_id: uuid.UUID | str) -> dict[str, Any]:
        return ensure_state(self.store.get_game_state(thread_id))

    def view(self, thread_id: uuid.UUID | str) -> dict[str, Any]:
        return state_view(self.load(thread_id))

    def commit(
        self,
        thread_id: uuid.UUID | str,
        turn_no: int,
        state_after: dict[str, Any],
        *,
        resolution: dict[str, Any] | None = None,
        applied_effects: list[dict[str, Any]] | None = None,
    ) -> None:
        self.store.save_game_state(
            thread_id,
            turn_no,
            state_after,
            kind=(
                "resolution"
                if resolution
                else "effects"
                if applied_effects
                else "state"
            ),
            payload={
                "resolution": resolution or {},
                "effects": applied_effects or [],
            },
        )
