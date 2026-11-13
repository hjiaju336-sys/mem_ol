from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.pipelines.memory import MemoryPipeline  # noqa: E402
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.services.dialogue import DialogueEngine  # noqa: E402
from mem_ol.storage.postgres import (  # noqa: E402
    PostgresStore,
    init_schema,
)

DEFAULT_MESSAGES = [
    "我在花果山脚下醒来，想先弄清这里是什么地方。",
    "我沿着溪水往山上走，遇到一群猴子。",
    "我向猴群打听水帘洞的入口。",
    "我决定钻进瀑布看看。",
    "洞里有石桌石碗，我想在这里住下。",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="GM 对话 + 5 轮增量入库演示")
    parser.add_argument(
        "--thread-name", default="西游·游戏记忆演示", help="线程名称"
    )
    parser.add_argument(
        "--worldview-file",
        default=str(ROOT / "data/worldview/xiyouji_001.txt"),
        help="世界观文本；为空则跳过",
    )
    parser.add_argument(
        "--turns", type=int, default=5, help="脚本模式下的用户回合数"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="交互式对话"
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    init_schema(settings.postgres_dsn, ROOT / "src/mem_ol/storage/schema.sql")

    store = PostgresStore(settings.postgres_dsn)
    try:
        thread = store.get_or_create_thread(args.thread_name)
        pipeline = MemoryPipeline(store)

        batches = store.list_batches(thread["id"])
        has_worldview = any(
            batch["kind"] == "worldview" and batch["status"] == "done"
            for batch in batches
        )
        if args.worldview_file and not has_worldview:
            print(f"[世界观] 入库 {args.worldview_file}")
            seeded = pipeline.ingest_worldview(
                thread["id"],
                Path(args.worldview_file).read_text(encoding="utf-8"),
                source_name=Path(args.worldview_file).name,
            )
            print(
                f"[世界观] batch={seeded['batch_seq']} "
                f"chunks={seeded['chunk_count']}"
            )

        engine = DialogueEngine(
            store, retrieval=RetrievalService(store, settings=settings)
        )

        messages = (
            None
            if args.interactive
            else DEFAULT_MESSAGES[: args.turns]
        )

        def run_turn(user_message: str) -> None:
            result = engine.chat(thread["id"], user_message)
            print(f"\n你> {user_message}")
            print(f"GM> {result['reply']}")
            if result["retrieved"]:
                print(
                    "召回> "
                    + ", ".join(
                        f"{item['chunk_no']}({item['score']:.3f})"
                        for item in result["retrieved"]
                    )
                )
            pending = store.count_un_ingested_turns(thread["id"])
            print(f"[未入库轮次] {pending}")
            if pending >= settings.memory_batch_turns:
                print("[入库] 触发 5 轮批次向量化…")
                done = pipeline.ingest_pending(thread["id"])
                for batch in done:
                    print(
                        f"[入库] batch={batch['batch_seq']} "
                        f"turns={batch['turn_start']}-{batch['turn_end']} "
                        f"chunks={batch['chunk_count']}"
                    )

        if messages is not None:
            for message in messages:
                run_turn(message)
        else:
            print("输入 q 退出。")
            while True:
                message = input("\n你> ").strip()
                if message.lower() in {"q", "quit", "exit"}:
                    break
                if message:
                    run_turn(message)

        print("\n[线程]")
        print(f"  thread_id={thread['id']}")
        for batch in store.list_batches(thread["id"]):
            print(
                f"  batch={batch['batch_seq']} kind={batch['kind']} "
                f"status={batch['status']} turns={batch['turn_start']}-"
                f"{batch['turn_end']} chunks={batch['chunk_count']}"
            )
        print(f"  剩余未入库轮次={store.count_un_ingested_turns(thread['id'])}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
