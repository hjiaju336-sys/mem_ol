from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.mcp_server.tools import get_tool_service  # noqa: E402


def _norm(name: str) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    return " ".join(text.split())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="合并同线程内规范名相同的重复实体（别名/属性/关系并入最早实体）"
    )
    parser.add_argument("--thread-id", default=None, help="线程 UUID")
    parser.add_argument("--thread-name", default=None, help="线程名称")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际执行合并；默认只打印分组（dry-run）",
    )
    args = parser.parse_args()
    if not args.thread_id and not args.thread_name:
        parser.error("需要 --thread-id 或 --thread-name")

    setup_logging()
    tools = get_tool_service()
    store = tools.store
    if args.thread_id:
        thread = store.get_thread(args.thread_id)
    else:
        thread = store.get_or_create_thread(args.thread_name)
    if thread is None:
        raise SystemExit("线程不存在")
    tid = str(thread["id"])

    entities = tools.neo4j.list_entities(tid)
    groups: dict[str, list[dict]] = {}
    for entity in entities:
        groups.setdefault(_norm(entity["canonical_name"]), []).append(entity)

    duplicates = {
        key: sorted(
            group,
            key=lambda item: (
                int(item.get("created_turn_no") or 0),
                str(item["entity_id"]),
            ),
        )
        for key, group in groups.items()
        if len(group) > 1
    }
    if not duplicates:
        print(f"线程 {thread['name']} 没有规范名重复的实体（{len(entities)} 个）")
        return

    for key, group in duplicates.items():
        survivor = group[0]
        others = group[1:]
        print(f"\n[{key}] 合并 {len(group)} 个实体")
        print(
            f"  保留: {survivor['canonical_name']} "
            f"({survivor['entity_id']}, turn={survivor.get('created_turn_no')})"
        )
        for item in others:
            print(
                f"  并入: {item['canonical_name']} "
                f"({item['entity_id']}, turn={item.get('created_turn_no')})"
            )
        if not args.apply:
            continue
        result = tools.neo4j.merge_entities(
            tid,
            survivor["entity_id"],
            [item["entity_id"] for item in others],
        )
        store.delete_entity_index(
            tid, [str(item["entity_id"]) for item in others]
        )
        print(
            f"  完成: 别名 {len(result['aliases'])} 个，"
            f"迁移属性 {result['moved_attributes']}，"
            f"迁移关系 {result['moved_relations']}"
        )

    if not args.apply:
        print("\n以上为 dry-run；确认后加 --apply 执行。")
    else:
        remaining = tools.neo4j.list_entities(tid)
        print(f"\n合并后实体数: {len(remaining)}")


if __name__ == "__main__":
    main()
