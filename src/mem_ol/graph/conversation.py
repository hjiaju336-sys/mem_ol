from __future__ import annotations

import threading
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services.dialogue import DialogueEngine
from mem_ol.services.pre_turn import PreTurnService
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class ConversationState(TypedDict, total=False):
    thread_id: str
    turn_no: int
    user_message: str
    pre_turn: dict[str, Any]
    query_analysis: dict[str, Any]
    pre_turn_error: str | None
    reply: str
    history_messages: int
    retrieved: list[dict[str, Any]]
    entity_candidates: list[dict[str, Any]]
    graph_paths: list[dict[str, Any]]
    trace_id: str | None
    message_ids: list[str]
    pending_turns: int
    ingest_required: bool
    commit_required: bool
    turn_checkpoint_id: str | None


class ConversationGraph:
    """一轮对话的 LangGraph 编排：prepare → pre_turn → generate → persist。"""

    def __init__(
        self,
        store: PostgresStore,
        tools: EntityToolService,
        settings: Settings | None = None,
        checkpointer: Any | None = None,
        pre_turn_service: PreTurnService | None = None,
        dialogue_engine: DialogueEngine | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.pre_turn_service = pre_turn_service or PreTurnService(
            tools, settings=self.settings
        )
        self.dialogue = dialogue_engine or DialogueEngine(
            store, settings=self.settings
        )
        self._lock = threading.Lock()
        self.graph = self._build(checkpointer)

    def _build(self, checkpointer: Any | None):
        builder = StateGraph(ConversationState)
        builder.add_node("prepare", self._prepare)
        builder.add_node("pre_turn", self._pre_turn)
        builder.add_node("generate", self._generate)
        builder.add_node("persist", self._persist)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "pre_turn")
        builder.add_edge("pre_turn", "generate")
        builder.add_edge("generate", "persist")
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=checkpointer)

    def run(
        self,
        thread_id: str,
        user_message: str,
        *,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        config = {"configurable": {"thread_id": str(thread_id)}}
        initial: ConversationState = {
            "thread_id": str(thread_id),
            "user_message": user_message,
        }
        with self._lock, log_context(thread_id=str(thread_id)):
            result = self.graph.invoke(initial, config)
            snapshot = self.graph.get_state(config)
            final_checkpoint_id = (
                snapshot.config.get("configurable", {}).get("checkpoint_id")
                if snapshot and snapshot.config
                else None
            )
            final_checkpoint_id = checkpoint_id or final_checkpoint_id
            if final_checkpoint_id:
                try:
                    self.store.update_turn_checkpoint(
                        thread_id,
                        int(result["turn_no"]),
                        final_checkpoint_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "checkpoint_id 回写失败",
                        extra={
                            "event": "graph.checkpoint.persist.failed",
                            "turn_no": result.get("turn_no"),
                        },
                    )
            result["turn_checkpoint_id"] = final_checkpoint_id
            try:
                cleanup = self.store.cleanup_checkpoints(
                    thread_id, self.settings.checkpoint_retention_turns
                )
                if cleanup["deleted_checkpoints"]:
                    logger.info(
                        "checkpoint 清理完成",
                        extra={
                            "event": "graph.checkpoint.cleanup",
                            **cleanup,
                        },
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "checkpoint 清理失败",
                    extra={"event": "graph.checkpoint.cleanup.failed"},
                )
            logger.info(
                "LangGraph 轮次完成",
                extra={
                    "event": "graph.turn.done",
                    "turn_no": result.get("turn_no"),
                    "checkpoint_id": final_checkpoint_id,
                    "pending_turns": result.get("pending_turns"),
                    "commit_required": result.get("commit_required"),
                },
            )
            return result

    # ---------- nodes ----------

    def _prepare(self, state: ConversationState) -> dict[str, Any]:
        thread_id = state["thread_id"]
        # checkpoint 会恢复上一轮 state；每轮必须从 DB 取新 turn_no，
        # 并清空上一轮的临时字段，避免第二轮复用旧的 turn_no/reply。
        turn_no = self.store.next_turn_no(thread_id)
        return {
            "turn_no": turn_no,
            "pre_turn": {},
            "query_analysis": {},
            "pre_turn_error": None,
            "reply": "",
            "history_messages": 0,
            "retrieved": [],
            "entity_candidates": [],
            "graph_paths": [],
            "trace_id": None,
            "message_ids": [],
            "pending_turns": 0,
            "ingest_required": False,
            "commit_required": False,
            "turn_checkpoint_id": None,
        }

    def _pre_turn(self, state: ConversationState) -> dict[str, Any]:
        try:
            packet = self.pre_turn_service.run(
                state["thread_id"],
                state["user_message"],
                turn_no=state["turn_no"],
            )
            return {
                "pre_turn": packet,
                "query_analysis": packet.get("query_analysis", {}),
                "trace_id": packet.get("trace_id"),
                "pre_turn_error": None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LangGraph pre_turn 失败，降级为纯上下文",
                extra={"event": "graph.pre_turn.failed", "error": str(exc)},
            )
            return {
                "pre_turn": {},
                "query_analysis": {},
                "trace_id": None,
                "pre_turn_error": str(exc),
            }

    def _generate(self, state: ConversationState) -> dict[str, Any]:
        packet = state.get("pre_turn") or None
        generated = self.dialogue.generate(
            state["thread_id"],
            state["user_message"],
            pre_turn=packet,
            turn_no=state["turn_no"],
        )
        return {
            **generated,
            "commit_required": bool(
                packet and (packet.get("trace_id") or packet.get("hypothetical_entities"))
            ),
        }

    def _persist(self, state: ConversationState) -> dict[str, Any]:
        message_ids = self.dialogue.persist_turn(
            state["thread_id"],
            state["user_message"],
            state["reply"],
            int(state["turn_no"]),
        )
        turn_no = int(state["turn_no"])
        if turn_no > self.settings.dialogue_history_turns:
            self.store.delete_traces_before_turn(
                state["thread_id"],
                turn_no - self.settings.dialogue_history_turns + 1,
            )
        pending = self.store.count_un_ingested_turns(state["thread_id"])
        return {
            "message_ids": message_ids,
            "pending_turns": pending,
            "ingest_required": pending >= self.settings.memory_batch_turns,
        }
