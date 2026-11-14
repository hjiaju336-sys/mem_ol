from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from mem_ol.api.schemas import (
    ChatRequest,
    LorebookPreviewRequest,
    RollbackRequest,
    ThreadCreate,
    WorldImportRequest,
    WorldviewRequest,
)
from mem_ol.config import Settings
from mem_ol.log import get_logger
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services.game_state import GameStateService
from mem_ol.services.llm import get_llm_service
from mem_ol.services.lorebook import parse_lorebook, select_entries
from mem_ol.services.opening import OpeningService
from mem_ol.services.pre_turn import PreTurnService
from mem_ol.services.rollback import RollbackService
from mem_ol.storage.postgres import PostgresStore

router = APIRouter(prefix="/api")
logger = get_logger(__name__)


def _store(request: Request) -> PostgresStore:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _check_lorebook_size(
    lorebook: dict[str, Any] | None, max_bytes: int
) -> None:
    if not lorebook:
        return
    try:
        size = len(
            json.dumps(lorebook, ensure_ascii=False).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="世界书不是合法 JSON") from exc
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"世界书超过 {max_bytes} 字节上限",
        )


def _tools(request: Request) -> EntityToolService:
    return request.app.state.tool_service


def _require_thread(
    store: PostgresStore, thread_id: uuid.UUID
) -> dict[str, Any]:
    thread = store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


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


@router.get("/threads/{thread_id}/state")
def get_game_state(
    thread_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    return GameStateService(store).view(thread_id)


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


@router.post("/lorebook/preview")
def lorebook_preview(
    payload: LorebookPreviewRequest, request: Request
) -> dict[str, Any]:
    """无状态预览：不保存世界书，只返回本轮会命中的条目。"""
    settings = _settings(request)
    _check_lorebook_size(payload.lorebook, settings.lorebook_max_bytes)
    try:
        book = parse_lorebook(payload.lorebook)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        entries = select_entries(
            book,
            payload.text,
            recent_messages=payload.recent_messages,
            scan_depth=settings.lorebook_scan_depth,
            max_chars=settings.lorebook_max_chars,
            recursion_steps=settings.lorebook_recursion_steps,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"世界书触发失败：{exc}"
        ) from exc
    return {
        "name": book.name,
        "format": book.source_format,
        "entry_count": len(book.entries),
        "active": [
            {
                "uid": entry.uid,
                "keys": entry.keys,
                "order": entry.order,
                "constant": entry.constant,
                "position": entry.position,
            }
            for entry in entries
        ],
    }


@router.post("/threads/{thread_id}/messages")
def chat(
    thread_id: uuid.UUID,
    payload: ChatRequest,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    settings = _settings(request)
    _require_thread(store, thread_id)

    client_context = (
        payload.client_context.model_dump()
        if payload.client_context is not None
        else None
    )
    _check_lorebook_size(
        (client_context or {}).get("lorebook"),
        settings.lorebook_max_bytes,
    )
    state = request.app.state.conversation_graph.run(
        str(thread_id), payload.content, client_context=client_context
    )

    if state.get("channel") == "meta":
        try:
            current_view = GameStateService(store).view(thread_id)
        except Exception:  # noqa: BLE001
            current_view = {}
        return {
            "reply": "",
            "channel": "meta",
            "turn_no": None,
            "intent": "meta",
            "meta_kind": state.get("meta_kind", "instruction"),
            "meta_reply": state.get("meta_reply", ""),
            "directives": state.get("directives", {}),
            "lorebook_active": [],
            "message_ids": [],
            "state_view": current_view,
            "resolution": None,
            "pending_turns": 0,
            "ingest_scheduled": False,
            "commit_scheduled": False,
        }

    turn_no = int(state["turn_no"])
    ingest_scheduled = bool(
        state.get("ingest_required")
        and store.enqueue_background_task(
            thread_id, "ingest", {}, f"turn:{turn_no}"
        )
    )

    packet = state.get("pre_turn") or {}
    commit_scheduled = bool(
        state.get("commit_required")
        and packet
        and store.enqueue_background_task(
            thread_id,
            "commit",
            {
                "user_message": state.get("narrative_message")
                or payload.content,
                "gm_reply": state.get("reply", ""),
                "packet": packet,
                "turn_no": turn_no,
                "message_ids": state.get("message_ids", []),
            },
            f"turn:{turn_no}",
        )
    )

    store.enqueue_background_task(
        thread_id, "plot", {"turn_no": turn_no}, f"turn:{turn_no}"
    )

    return {
        "reply": state.get("reply", ""),
        "channel": "narrative",
        "turn_no": state.get("turn_no"),
        "intent": state.get("intent", "default"),
        "meta_kind": None,
        "meta_reply": None,
        "directives": (state.get("pre_turn") or {}).get("directives", {}),
        "lorebook_active": state.get("lorebook_active", []),
        "state_view": state.get("state_view", {}),
        "resolution": state.get("resolution"),
        "applied_effects": state.get("applied_effects", []),
        "effects_rejected": state.get("effects_rejected", []),
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
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    pending = store.count_un_ingested_turns(thread_id)
    task_id = store.enqueue_background_task(
        thread_id, "ingest", {}, f"manual:{uuid.uuid4()}"
    )
    return {
        "status": "scheduled",
        "pending_turns": pending,
        "task_id": task_id,
    }


@router.post("/threads/{thread_id}/import-world")
def import_world(
    thread_id: uuid.UUID,
    payload: WorldImportRequest,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    digest = hashlib.sha1(payload.text.encode("utf-8")).hexdigest()[:16]
    task_id = store.enqueue_background_task(
        thread_id,
        "world_import",
        {
            "text": payload.text,
            "source_name": payload.source_name,
            "append": payload.append,
            "doc_id": payload.doc_id,
            "char_start": payload.char_start,
            "char_end": payload.char_end,
        },
        f"{digest}:{payload.doc_id or ''}:"
        f"{payload.char_start}:{payload.char_end or ''}",
    )
    return {
        "status": "scheduled",
        "chars": len(payload.text),
        "task_id": task_id,
    }


@router.post("/threads/{thread_id}/worldview")
def add_worldview(
    thread_id: uuid.UUID,
    payload: WorldviewRequest,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    _require_thread(store, thread_id)
    digest = hashlib.sha1(payload.text.encode("utf-8")).hexdigest()[:16]
    task_id = store.enqueue_background_task(
        thread_id,
        "worldview",
        {"text": payload.text, "source_name": payload.source_name},
        digest,
    )
    return {
        "status": "scheduled",
        "chars": len(payload.text),
        "task_id": task_id,
    }


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
