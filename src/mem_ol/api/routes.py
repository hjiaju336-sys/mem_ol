from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from mem_ol.config import Settings
from mem_ol.log import get_logger
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.pipelines.memory import MemoryPipeline
from mem_ol.services.entity_commit import EntityCommitService
from mem_ol.services.llm import LLMService, get_llm_service
from mem_ol.services.opening import OpeningService
from mem_ol.services.plot_memory import PlotMemoryService
from mem_ol.services.pre_turn import PreTurnService
from mem_ol.services.rollback import RollbackService
from mem_ol.services.world_import import WorldImportService
from mem_ol.storage.postgres import PostgresStore

router = APIRouter(prefix="/api")
logger = get_logger(__name__)


class ThreadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)
    checkpoint_id: str | None = None


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


def _store(request: Request) -> PostgresStore:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _tools(request: Request) -> EntityToolService:
    return request.app.state.tool_service


def _require_thread(
    store: PostgresStore, thread_id: uuid.UUID
) -> dict[str, Any]:
    thread = store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


def _run_ingest(
    store: PostgresStore,
    settings: Settings,
    lock,
    thread_id: uuid.UUID,
) -> None:
    with lock:
        try:
            MemoryPipeline(store, settings=settings).ingest_pending(thread_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "后台对话入库失败",
                extra={
                    "event": "memory.ingest.background.failed",
                    "thread_id": str(thread_id),
                },
            )


def _run_worldview(
    store: PostgresStore,
    settings: Settings,
    lock,
    thread_id: uuid.UUID,
    text: str,
    source_name: str | None,
) -> None:
    with lock:
        try:
            MemoryPipeline(store, settings=settings).ingest_worldview(
                thread_id, text, source_name
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "后台世界观入库失败",
                extra={
                    "event": "memory.worldview.background.failed",
                    "thread_id": str(thread_id),
                },
            )


def _run_world_import(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    lock,
    thread_id: uuid.UUID,
    text: str,
    source_name: str | None,
    append: bool = False,
    doc_id: str | None = None,
    char_start: int = 0,
    char_end: int | None = None,
) -> None:
    with lock:
        try:
            WorldImportService(tools, settings=settings, llm=llm).import_world(
                str(thread_id),
                text,
                source_name,
                append=append,
                doc_id=doc_id,
                char_start=char_start,
                char_end=char_end,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "后台世界导入失败",
                extra={
                    "event": "world.import.background.failed",
                    "thread_id": str(thread_id),
                },
            )


def _run_plot_maintenance(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    lock,
    thread_id: uuid.UUID,
    turn_no: int,
) -> None:
    with lock:
        try:
            PlotMemoryService(
                tools.store, tools.embedder, llm, settings
            ).maintain(thread_id, turn_no)
        except Exception:  # noqa: BLE001
            logger.exception(
                "剧情摘要维护失败",
                extra={
                    "event": "plot.maintenance.failed",
                    "thread_id": str(thread_id),
                    "turn_no": turn_no,
                },
            )


def _run_commit(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    thread_id: uuid.UUID,
    user_message: str,
    gm_reply: str,
    packet: dict[str, Any],
    turn_no: int,
    message_ids: list[str],
) -> None:
    try:
        EntityCommitService(tools, llm, settings).run(
            thread_id=thread_id,
            user_message=user_message,
            gm_reply=gm_reply,
            packet=packet,
            turn_no=turn_no,
            message_ids=message_ids,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "后台实体固化失败",
            extra={
                "event": "api3.commit.background.failed",
                "thread_id": str(thread_id),
                "turn_no": turn_no,
            },
        )


@router.post("/threads")
def create_thread(
    payload: ThreadCreate, request: Request
) -> dict[str, Any]:
    return _store(request).get_or_create_thread(payload.name)


@router.get("/threads")
def list_threads(request: Request) -> list[dict[str, Any]]:
    return _store(request).list_threads()


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    return _require_thread(_store(request), thread_id)


@router.get("/threads/{thread_id}/messages")
def list_messages(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_messages(thread_id)


@router.post("/threads/{thread_id}/opening")
def opening(
    thread_id: uuid.UUID,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    result = OpeningService(store, settings=_settings(request)).generate(
        thread_id
    )
    return result or {"reply": None, "turn_no": 0}


@router.post("/threads/{thread_id}/pre-turn")
def pre_turn(
    thread_id: uuid.UUID,
    payload: ChatRequest,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    turn_no = store.next_turn_no(thread_id)
    return PreTurnService(
        _tools(request),
        store=store,
        llm=get_llm_service(),
        settings=_settings(request),
    ).run(thread_id, payload.content, turn_no=turn_no)


@router.post("/threads/{thread_id}/messages")
def chat(
    thread_id: uuid.UUID,
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    settings = _settings(request)
    tools = _tools(request)
    llm = get_llm_service()
    _require_thread(store, thread_id)

    state = request.app.state.conversation_graph.run(
        str(thread_id), payload.content
    )

    ingest_scheduled = bool(state.get("ingest_required"))
    if ingest_scheduled:
        background_tasks.add_task(
            _run_ingest,
            store,
            settings,
            request.app.state.ingest_lock,
            thread_id,
        )

    packet = state.get("pre_turn") or {}
    commit_scheduled = bool(state.get("commit_required") and packet)
    if commit_scheduled:
        background_tasks.add_task(
            _run_commit,
            tools,
            llm,
            settings,
            thread_id,
            payload.content,
            state["reply"],
            packet,
            int(state["turn_no"]),
            state.get("message_ids", []),
        )

    background_tasks.add_task(
        _run_plot_maintenance,
        tools,
        llm,
        settings,
        request.app.state.ingest_lock,
        thread_id,
        int(state["turn_no"]),
    )

    return {
        "reply": state.get("reply", ""),
        "turn_no": state.get("turn_no"),
        "history_messages": state.get("history_messages", 0),
        "retrieved": state.get("retrieved", []),
        "trace_id": state.get("trace_id"),
        "message_ids": state.get("message_ids", []),
        "entity_candidates": state.get("entity_candidates", []),
        "graph_paths": state.get("graph_paths", []),
        "query_analysis": state.get("query_analysis", {}),
        "checkpoint_id": state.get("turn_checkpoint_id"),
        "pre_turn_error": state.get("pre_turn_error"),
        "pending_turns": state.get("pending_turns", 0),
        "ingest_scheduled": ingest_scheduled,
        "commit_scheduled": commit_scheduled,
    }


@router.post("/threads/{thread_id}/rollback")
def rollback(
    thread_id: uuid.UUID,
    payload: RollbackRequest,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    tools = _tools(request)
    _require_thread(store, thread_id)
    try:
        return RollbackService(
            store, tools.neo4j, settings=_settings(request)
        ).rollback(thread_id, payload.target_turn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/ingest")
def ingest_pending(
    thread_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    settings = _settings(request)
    _require_thread(store, thread_id)
    pending = store.count_un_ingested_turns(thread_id)
    background_tasks.add_task(
        _run_ingest,
        store,
        settings,
        request.app.state.ingest_lock,
        thread_id,
    )
    return {"status": "scheduled", "pending_turns": pending}


@router.post("/threads/{thread_id}/import-world")
def import_world(
    thread_id: uuid.UUID,
    payload: WorldImportRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    background_tasks.add_task(
        _run_world_import,
        _tools(request),
        get_llm_service(),
        _settings(request),
        request.app.state.ingest_lock,
        thread_id,
        payload.text,
        payload.source_name,
        payload.append,
        payload.doc_id,
        payload.char_start,
        payload.char_end,
    )
    return {"status": "scheduled", "chars": len(payload.text)}


@router.post("/threads/{thread_id}/worldview")
def add_worldview(
    thread_id: uuid.UUID,
    payload: WorldviewRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    background_tasks.add_task(
        _run_worldview,
        store,
        request.app.state.settings,
        request.app.state.ingest_lock,
        thread_id,
        payload.text,
        payload.source_name,
    )
    return {"status": "scheduled", "chars": len(payload.text)}


@router.get("/threads/{thread_id}/batches")
def list_batches(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_batches(thread_id)


@router.get("/threads/{thread_id}/chunks")
def list_chunks(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_chunks(thread_id)


@router.get("/threads/{thread_id}/entities")
def list_entities(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_entity_index(thread_id)


@router.get("/threads/{thread_id}/entity-mentions")
def list_entity_mentions(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_entity_mentions(thread_id)


@router.get("/threads/{thread_id}/traces")
def list_traces(
    thread_id: uuid.UUID, request: Request
) -> list[dict[str, Any]]:
    store = _store(request)
    _require_thread(store, thread_id)
    return store.list_retrieval_traces(thread_id)
