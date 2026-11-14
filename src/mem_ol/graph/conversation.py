from __future__ import annotations

import threading
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger, log_context
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services.dialogue import DialogueEngine
from mem_ol.services.game_state import GameStateService
from mem_ol.services.numeric_engine import (
    apply_effects,
    ensure_actor,
    resolve_action,
    state_view,
)
from mem_ol.services.pre_turn import PreTurnService
from mem_ol.services.state_judge import StateJudge
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class ConversationState(TypedDict, total=False):
    thread_id: str
    turn_no: int
    user_message: str
    narrative_message: str
    channel: str
    intent: str
    meta_kind: str
    meta_reply: str
    analysis: Any
    pre_turn: dict[str, Any]
    state_view: dict[str, Any]
    resolution: dict[str, Any] | None
    state_after: dict[str, Any]
    effects: list[dict[str, Any]]
    appearances: list[str]
    applied_effects: list[dict[str, Any]]
    effects_rejected: list[dict[str, Any]]
    query_analysis: dict[str, Any]
    pre_turn_error: str | None
    reply: str
    history_messages: int
    retrieved: list[dict[str, Any]]
    entity_candidates: list[dict[str, Any]]
    graph_paths: list[dict[str, Any]]
    lorebook_active: list[str]
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
        game_state_service: GameStateService | None = None,
        state_judge: StateJudge | None = None,
    ) -> None:
        self.store = store
        self.tools = tools
        self.settings = settings or get_settings()
        self.pre_turn_service = pre_turn_service or PreTurnService(
            tools, settings=self.settings
        )
        self.dialogue = dialogue_engine or DialogueEngine(
            store, settings=self.settings
        )
        self.game_state = game_state_service or GameStateService(store)
        self.state_judge = state_judge or StateJudge(settings=self.settings)
        self._lock = threading.Lock()
        # 玩家侧内容不进 LangGraph state，避免被 checkpoint 落库。
        self._client_context: dict[str, Any] = {}
        self.graph = self._build(checkpointer)

    def _build(self, checkpointer: Any | None):
        builder = StateGraph(ConversationState)
        builder.add_node("prepare", self._prepare)
        builder.add_node("pre_turn", self._pre_turn)
        builder.add_node("state", self._state)
        builder.add_node("generate", self._generate)
        builder.add_node("judge", self._judge)
        builder.add_node("reconcile", self._reconcile)
        builder.add_node("persist", self._persist)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "pre_turn")
        builder.add_conditional_edges(
            "pre_turn",
            self._route_after_pre_turn,
            {"state": "state", "end": END},
        )
        builder.add_edge("state", "generate")
        builder.add_edge("generate", "judge")
        builder.add_edge("judge", "reconcile")
        builder.add_edge("reconcile", "persist")
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=checkpointer)

    def _route_after_pre_turn(self, state: ConversationState) -> str:
        packet = state.get("pre_turn") or {}
        return "end" if packet.get("channel") == "meta" else "state"

    def run(
        self,
        thread_id: str,
        user_message: str,
        *,
        checkpoint_id: str | None = None,
        client_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = {"configurable": {"thread_id": str(thread_id)}}
        pending_instructions = [
            str(item)
            for item in ((client_context or {}).get("pending_instructions") or [])
            if str(item).strip()
        ]
        initial: ConversationState = {
            "thread_id": str(thread_id),
            "user_message": user_message,
        }
        with self._lock, log_context(thread_id=str(thread_id)):
            analysis = None
            classifier = getattr(self.pre_turn_service, "classify", None)
            if callable(classifier):
                try:
                    analysis = classifier(
                        thread_id,
                        user_message,
                        pending_instructions=pending_instructions,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "输入分流失败，按叙事处理",
                        extra={"event": "graph.classify.failed", "error": str(exc)},
                    )
            if analysis is not None and analysis.channel == "meta":
                meta_reply = ""
                if analysis.meta_kind == "question":
                    meta_reply = self.pre_turn_service.meta_reply(
                        analysis.meta_question or user_message,
                        recent=self.store.get_recent_turns(
                            thread_id, self.settings.dialogue_history_turns
                        ),
                    )
                logger.info(
                    "元交互完成",
                    extra={
                        "event": "graph.meta.done",
                        "meta_kind": analysis.meta_kind,
                        "question_chars": len(analysis.meta_question or ""),
                        "reply_chars": len(meta_reply),
                    },
                )
                return {
                    "thread_id": str(thread_id),
                    "turn_no": None,
                    "channel": "meta",
                    "intent": "meta",
                    "meta_kind": analysis.meta_kind,
                    "meta_reply": meta_reply,
                    "reply": "",
                    "history_messages": 0,
                    "retrieved": [],
                    "entity_candidates": [],
                    "graph_paths": [],
                    "lorebook_active": [],
                    "trace_id": None,
                    "message_ids": [],
                    "pending_turns": 0,
                    "ingest_required": False,
                    "commit_required": False,
                    "turn_checkpoint_id": None,
                    "query_analysis": {
                        "channel": "meta",
                        "intent": "meta",
                        "meta_kind": analysis.meta_kind,
                        "directives": analysis.directives.model_dump(),
                    },
                    "directives": analysis.directives.model_dump(),
                }
            initial["analysis"] = (
                analysis.model_dump()
                if hasattr(analysis, "model_dump")
                else analysis
            )
            self._client_context = client_context or {}
            try:
                result = self.graph.invoke(initial, config)
            finally:
                self._client_context = {}
            snapshot = self.graph.get_state(config)
            final_checkpoint_id = (
                snapshot.config.get("configurable", {}).get("checkpoint_id")
                if snapshot and snapshot.config
                else None
            )
            final_checkpoint_id = checkpoint_id or final_checkpoint_id
            turn_no = result.get("turn_no")
            if (
                final_checkpoint_id
                and turn_no is not None
                and result.get("channel") != "meta"
            ):
                try:
                    self.store.update_turn_checkpoint(
                        thread_id,
                        int(turn_no),
                        final_checkpoint_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "checkpoint_id 回写失败",
                        extra={
                            "event": "graph.checkpoint.persist.failed",
                            "turn_no": turn_no,
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
                    "turn_no": turn_no,
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
            "state_view": {},
            "resolution": None,
            "state_after": {},
            "effects": [],
            "applied_effects": [],
            "effects_rejected": [],
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
            kwargs: dict[str, Any] = {"turn_no": state["turn_no"]}
            analysis = state.get("analysis")
            if analysis is not None:
                kwargs["analysis"] = analysis
            pending = [
                str(item)
                for item in (
                    (self._client_context or {}).get("pending_instructions")
                    or []
                )
                if str(item).strip()
            ]
            if pending:
                kwargs["pending_instructions"] = pending
            try:
                time_view = self.game_state.view(state["thread_id"])
                time_label = (time_view.get("world") or {}).get("time_label")
                if time_label:
                    kwargs["time_label"] = time_label
            except Exception:  # noqa: BLE001
                pass
            packet = self.pre_turn_service.run(
                state["thread_id"],
                state["user_message"],
                **kwargs,
            )
            if packet.get("channel") == "meta":
                meta_reply = ""
                if packet.get("meta_kind") == "question":
                    meta_reply = self.pre_turn_service.meta_reply(
                        packet.get("meta_question") or state["user_message"],
                        recent=self.store.get_recent_turns(
                            state["thread_id"],
                            self.settings.dialogue_history_turns,
                        ),
                    )
                return {
                    "pre_turn": packet,
                    "channel": "meta",
                    "intent": "meta",
                    "meta_kind": packet.get("meta_kind", "instruction"),
                    "meta_reply": meta_reply,
                    "directives": (packet.get("query_analysis") or {}).get(
                        "directives", {}
                    ),
                    "query_analysis": packet.get("query_analysis", {}),
                    "trace_id": packet.get("trace_id"),
                    "pre_turn_error": None,
                }
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

    def _bind_entities(
        self,
        thread_id: str,
        game_state: dict[str, Any],
        names: list[Any],
    ) -> dict[str, Any]:
        """把角色状态挂到实体：按规范名/别名精确匹配，查不到就保持未绑定。"""
        actors = game_state.get("actors") or {}
        for name in {str(item or "").strip() for item in names}:
            if not name or name in ("player", "你", "玩家"):
                continue
            try:
                resolved = self.tools.entity_resolve(thread_id, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "实体绑定失败，跳过",
                    extra={
                        "event": "state.entity.bind.failed",
                        "name": name,
                        "error": str(exc),
                    },
                )
                continue
            if not resolved.get("found"):
                continue
            entity = resolved["entity"]
            for actor in actors.values():
                if actor.get("name") == name:
                    actor["entity_id"] = str(entity["entity_id"])
        return game_state

    def _sync_appearing_characters(
        self,
        thread_id: str,
        game_state: dict[str, Any],
        turn_no: int,
    ) -> dict[str, Any]:
        """上一轮 API 3 新建的角色，下一轮开始时补进状态并绑定实体。"""
        if self.tools is None or turn_no <= 1:
            return game_state
        try:
            entities = self.tools.characters_created_at(thread_id, turn_no - 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "上轮新角色同步失败，跳过",
                extra={
                    "event": "state.actors.sync.failed",
                    "error": str(exc),
                },
            )
            return game_state
        for entity in entities:
            name = str(entity.get("canonical_name") or "").strip()
            if not name:
                continue
            ensure_actor(
                game_state, name, str(entity.get("entity_id") or "")
            )
        return game_state

    def _state(self, state: ConversationState) -> dict[str, Any]:
        packet = state.get("pre_turn") or {}
        try:
            current = self.game_state.load(state["thread_id"])
            current = self._sync_appearing_characters(
                state["thread_id"], current, int(state["turn_no"])
            )
            resolution = None
            action = packet.get("action") or {}
            if action.get("needs_check"):
                current, resolution = resolve_action(
                    current,
                    action,
                    thread_id=state["thread_id"],
                    turn_no=int(state["turn_no"]),
                )
            current = self._bind_entities(
                state["thread_id"], current, [action.get("target")]
            )
            return {
                "state_view": state_view(current),
                "resolution": resolution,
                "state_after": current,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "数值状态结算失败，降级为无状态",
                extra={"event": "state.resolve.failed", "error": str(exc)},
            )
            return {"state_view": {}, "resolution": None, "state_after": {}}

    def _generate(self, state: ConversationState) -> dict[str, Any]:
        packet = state.get("pre_turn") or None
        narrative_message = (
            (packet or {}).get("narrative_message")
            or state["user_message"]
        )
        generated = self.dialogue.generate(
            state["thread_id"],
            narrative_message,
            pre_turn=packet,
            turn_no=state["turn_no"],
            client_context=self._client_context or {},
            state_view=state.get("state_view"),
            resolution=state.get("resolution"),
        )
        return {
            **generated,
            "narrative_message": narrative_message,
            "commit_required": bool(
                packet and (packet.get("trace_id") or packet.get("hypothetical_entities"))
            ),
        }

    def _judge(self, state: ConversationState) -> dict[str, Any]:
        """独立状态裁决：GM 正文之后、写库之前，产出 effects。"""
        reply = str(state.get("reply") or "").strip()
        if not reply:
            return {"effects": [], "appearances": []}
        recent = self.store.get_recent_turns(
            state["thread_id"], self.settings.dialogue_history_turns
        )
        recent_context = "\n".join(
            f"[{'玩家' if message['role'] == 'user' else 'GM'}] "
            f"{message['content']}"
            for message in recent
            if message["role"] in ("user", "assistant")
        )
        decision = self.state_judge.propose(
            user_action=state.get("narrative_message") or state["user_message"],
            reply=reply,
            state_view=state.get("state_view"),
            resolution=state.get("resolution"),
            recent_context=recent_context,
        )
        return {
            "effects": decision.get("effects", []),
            "appearances": decision.get("appearances", []),
        }

    def _reconcile(self, state: ConversationState) -> dict[str, Any]:
        effects = state.get("effects") or []
        appearances = state.get("appearances") or []
        state_after = state.get("state_after") or {}
        if not state_after:
            return {"applied_effects": [], "effects_rejected": []}
        try:
            for name in appearances:
                cleaned = str(name or "").strip()
                if cleaned and cleaned not in ("player", "你", "玩家"):
                    ensure_actor(state_after, cleaned)
            applied: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            if effects:
                blocked = {
                    str(item.get("path"))
                    for item in (state.get("resolution") or {}).get("deltas", [])
                    if item.get("path")
                }
                state_after, applied, rejected = apply_effects(
                    state_after, effects, blocked_paths=blocked
                )
            names = (
                [effect.get("target") for effect in effects]
                + [((state.get("resolution") or {}).get("action") or {}).get("target")]
                + list(appearances)
            )
            new_state = self._bind_entities(
                state["thread_id"], state_after, names
            )
            logger.info(
                "剧情后果结算完成",
                extra={
                    "event": "state.reconcile.done",
                    "applied": len(applied),
                    "rejected": len(rejected),
                    "appearances": len(appearances),
                },
            )
            return {
                "state_after": new_state,
                "state_view": state_view(new_state),
                "applied_effects": applied,
                "effects_rejected": rejected,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "剧情后果结算失败",
                extra={"event": "state.reconcile.failed", "error": str(exc)},
            )
            return {"applied_effects": [], "effects_rejected": []}

    def _persist(self, state: ConversationState) -> dict[str, Any]:
        message_ids = self.dialogue.persist_turn(
            state["thread_id"],
            state.get("narrative_message") or state["user_message"],
            state["reply"],
            int(state["turn_no"]),
        )
        turn_no = int(state["turn_no"])
        state_after = state.get("state_after")
        if state_after:
            try:
                self.game_state.commit(
                    state["thread_id"],
                    turn_no,
                    state_after,
                    resolution=state.get("resolution"),
                    applied_effects=state.get("applied_effects") or [],
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "游戏状态落库失败",
                    extra={
                        "event": "state.commit.failed",
                        "turn_no": turn_no,
                    },
                )
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
