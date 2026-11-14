from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres import PostgresSaver

from mem_ol.api.routes import router
from mem_ol.config import get_settings
from mem_ol.graph.conversation import ConversationGraph
from mem_ol.log import get_logger, setup_logging
from mem_ol.mcp_server.tools import close_tool_service, get_tool_service
from mem_ol.services.embedding import get_embedding_service
from mem_ol.services.llm import get_llm_service
from mem_ol.services.rerank import get_rerank_service
from mem_ol.services.task_worker import BackgroundWorker

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    with ExitStack() as stack:
        tool_service = get_tool_service()
        app.state.tool_service = tool_service
        app.state.store = tool_service.store
        app.state.settings = tool_service.settings

        checkpointer = stack.enter_context(
            PostgresSaver.from_conn_string(settings.postgres_dsn)
        )
        checkpointer.setup()
        app.state.conversation_graph = ConversationGraph(
            store=tool_service.store,
            tools=tool_service,
            settings=settings,
            checkpointer=checkpointer,
        )

        for model_name, service in (
            ("embedding", get_embedding_service()),
            ("reranker", get_rerank_service()),
        ):
            try:
                service.warmup()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "模型预热失败",
                    extra={
                        "event": "model.warmup.failed",
                        "model": model_name,
                    },
                    exc_info=True,
                )

        recovered = tool_service.store.reset_running_background_tasks()
        if recovered:
            logger.info(
                "回收上次未完成的后台任务",
                extra={"event": "background.worker.recovered", "count": recovered},
            )
        worker = BackgroundWorker(
            tool_service.store,
            tool_service,
            get_llm_service(),
            settings,
        )
        await worker.start()
        app.state.worker = worker

        try:
            yield
        finally:
            await worker.stop()
            close_tool_service()


app = FastAPI(
    title="mem_ol GM Demo",
    description="游戏化叙事记忆 Demo：GM 对话 + 增量向量化 + 实体召回",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
