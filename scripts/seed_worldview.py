from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.mcp_server.tools import (  # noqa: E402
    close_tool_service,
    get_tool_service,
)
from mem_ol.services.llm import get_llm_service  # noqa: E402
from mem_ol.services.world_import import WorldImportService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="导入世界：批量向量化上下文并初始化实体/关系"
    )
    parser.add_argument("text_file", help="世界文本路径")
    parser.add_argument(
        "--thread-name", default="西游·世界导入演示", help="线程名称"
    )
    parser.add_argument("--source-name", default=None, help="来源名称")
    parser.add_argument(
        "--append", action="store_true", help="追加到已有实体的会话"
    )
    parser.add_argument("--doc-id", default=None, help="同一文档的稳定 ID")
    parser.add_argument(
        "--char-start", type=int, default=0, help="本文在文档中的起始位置"
    )
    parser.add_argument(
        "--char-end", type=int, default=None, help="本文在文档中的结束位置"
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    text_path = Path(args.text_file)
    text = text_path.read_text(encoding="utf-8")

    tools = get_tool_service()
    store = tools.store
    try:
        thread = store.get_or_create_thread(args.thread_name)
        importer = WorldImportService(
            tools,
            store=store,
            settings=settings,
            llm=get_llm_service(),
        )
        try:
            summary = importer.import_world(
                str(thread["id"]),
                text,
                source_name=args.source_name or text_path.name,
                append=args.append,
                doc_id=args.doc_id,
                char_start=args.char_start,
                char_end=args.char_end,
            )
        except ValueError as exc:
            print(f"导入失败: {exc}")
            raise SystemExit(2) from exc

        summary["thread_name"] = thread["name"]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        entities = store.list_entity_index(thread["id"])
        print("entities:")
        for entity in entities[:30]:
            print(
                f"  {entity['canonical_name']:<12} "
                f"{entity['entity_type']:<10} "
                f"aliases={entity['aliases']}"
            )
    finally:
        close_tool_service()


if __name__ == "__main__":
    main()
