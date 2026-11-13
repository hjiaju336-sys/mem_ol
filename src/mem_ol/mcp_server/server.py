from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from mem_ol.config import get_settings
from mem_ol.mcp_server.tools import get_tool_service

mcp = MCPServer(
    "mem_ol-entity-tools",
    version="0.1.0",
    instructions=(
        "只暴露需要模型决策后的实体/关系写入工具。"
        "最近上下文、召回、实体详情等确定性读取由编排层服务完成，不经 MCP。"
    ),
)


@mcp.tool()
def entity_commit_plan(
    thread_id: str,
    trace_id: str,
    turn_no: int | None,
    batch_id: str | None,
    occurred_at: str | None,
    decisions: list[dict[str, Any]],
    source_chunk_nos: list[str] | None = None,
    source_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    return get_tool_service().entity_commit_plan(
        thread_id=thread_id,
        trace_id=trace_id,
        turn_no=turn_no,
        batch_id=batch_id,
        occurred_at=occurred_at,
        decisions=decisions,
        source_chunk_nos=source_chunk_nos,
        source_message_ids=source_message_ids,
    )


def main() -> None:
    settings = get_settings()
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
