from __future__ import annotations

from typing import Any

from mem_ol.config import Settings
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.pipelines.memory import MemoryPipeline
from mem_ol.services.entity_commit import EntityCommitService
from mem_ol.services.llm import LLMService
from mem_ol.services.plot_memory import PlotMemoryService
from mem_ol.services.world_import import WorldImportService
from mem_ol.storage.postgres import PostgresStore


def run_commit(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    thread_id: str,
    payload: dict[str, Any],
) -> None:
    EntityCommitService(tools, llm, settings).run(
        thread_id=thread_id,
        user_message=str(payload.get("user_message") or ""),
        gm_reply=str(payload.get("gm_reply") or ""),
        packet=payload.get("packet") or {},
        turn_no=payload.get("turn_no"),
        message_ids=payload.get("message_ids") or [],
    )


def run_ingest(
    store: PostgresStore,
    settings: Settings,
    thread_id: str,
) -> None:
    MemoryPipeline(store, settings=settings).ingest_pending(thread_id)


def run_plot(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    thread_id: str,
    payload: dict[str, Any],
) -> None:
    PlotMemoryService(tools.store, tools.embedder, llm, settings).maintain(
        thread_id, int(payload["turn_no"])
    )


def run_world_import(
    tools: EntityToolService,
    llm: LLMService,
    settings: Settings,
    thread_id: str,
    payload: dict[str, Any],
) -> None:
    WorldImportService(tools, settings=settings, llm=llm).import_world(
        str(thread_id),
        str(payload.get("text") or ""),
        payload.get("source_name"),
        append=bool(payload.get("append")),
        doc_id=payload.get("doc_id"),
        char_start=int(payload.get("char_start") or 0),
        char_end=payload.get("char_end"),
    )


def run_worldview(
    store: PostgresStore,
    settings: Settings,
    thread_id: str,
    payload: dict[str, Any],
) -> None:
    MemoryPipeline(store, settings=settings).ingest_worldview(
        thread_id,
        str(payload.get("text") or ""),
        payload.get("source_name"),
    )
