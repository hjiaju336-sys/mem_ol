from __future__ import annotations

import asyncio
from typing import Any

from mem_ol.config import Settings
from mem_ol.log import get_logger, log_context
from mem_ol.mcp_server.tools import EntityToolService
from mem_ol.services import maintenance
from mem_ol.services.llm import LLMService
from mem_ol.storage.postgres import PostgresStore

logger = get_logger(__name__)


class BackgroundWorker:
    """串行消费 background_tasks；失败退避重试，进程重启后继续。"""

    def __init__(
        self,
        store: PostgresStore,
        tools: EntityToolService,
        llm: LLMService,
        settings: Settings,
        *,
        interval_s: float = 2.0,
    ) -> None:
        self.store = store
        self.tools = tools
        self.llm = llm
        self.settings = settings
        self.interval_s = interval_s
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def run_once(self) -> bool:
        """领取并执行一个任务；没有可执行任务时返回 False。"""
        task = self.store.claim_background_task()
        if task is None:
            return False
        with log_context(thread_id=task["thread_id"]):
            try:
                self._dispatch(task)
            except Exception as exc:  # noqa: BLE001
                status = self.store.finish_background_task(
                    task["id"], error=str(exc)
                )
                logger.exception(
                    "后台任务失败",
                    extra={
                        "event": "background.task.failed",
                        "task_id": task["id"],
                        "kind": task["kind"],
                        "attempt": task["attempts"],
                        "status": status,
                        "error": str(exc),
                    },
                )
            else:
                self.store.finish_background_task(task["id"])
                logger.info(
                    "后台任务完成",
                    extra={
                        "event": "background.task.done",
                        "task_id": task["id"],
                        "kind": task["kind"],
                        "attempt": task["attempts"],
                    },
                )
        return True

    def _dispatch(self, task: dict[str, Any]) -> None:
        kind = task["kind"]
        thread_id = task["thread_id"]
        payload = task["payload"] or {}
        if kind == "commit":
            maintenance.run_commit(
                self.tools, self.llm, self.settings, thread_id, payload
            )
        elif kind == "ingest":
            maintenance.run_ingest(self.store, self.settings, thread_id)
        elif kind == "plot":
            maintenance.run_plot(
                self.tools, self.llm, self.settings, thread_id, payload
            )
        elif kind == "world_import":
            maintenance.run_world_import(
                self.tools, self.llm, self.settings, thread_id, payload
            )
        elif kind == "worldview":
            maintenance.run_worldview(
                self.store, self.settings, thread_id, payload
            )
        else:
            raise ValueError(f"未知后台任务类型: {kind}")

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "后台任务 worker 启动",
            extra={"event": "background.worker.start"},
        )

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await self._task

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            handled = await asyncio.to_thread(self.run_once)
            if handled:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_s
                )
            except asyncio.TimeoutError:
                pass
