from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.pipelines.memory import MemoryPipeline  # noqa: E402
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.storage.postgres import PostgresStore  # noqa: E402

TURNS = [
    (
        "我叫沈砚，我的佩剑叫青锋，剑柄上刻着一只白鹤。",
        "记住了，沈砚。青锋剑，剑柄刻鹤。",
    ),
    (
        "我在城南客栈欠了老板三钱银子。",
        "记下了，城南客栈，三钱银子。",
    ),
    (
        "我答应帮阿蘅找她失踪的哥哥。",
        "好，你答应了阿蘅。",
    ),
    (
        "我的左臂有一道旧伤，阴天会疼。",
        "明白，左臂旧伤。",
    ),
    (
        "我把铜钥匙藏在客栈床板下面。",
        "嗯，铜钥匙在床板下。",
    ),
    (
        "今天天气不错。",
        "确实不错。",
    ),
]

PROBES = [
    ("我的佩剑叫什么？剑柄上刻着什么？", "青锋", 1),
    ("我欠了客栈老板多少钱？", "三钱", 2),
    ("铜钥匙藏在哪里？", "床板", 5),
]


def main() -> None:
    setup_logging()
    settings = get_settings()
    store = PostgresStore(settings.postgres_dsn)
    thread = store.get_or_create_thread("对话记忆评测")
    thread_id = str(thread["id"])
    try:
        for turn_no, (user, assistant) in enumerate(TURNS, start=1):
            store.add_message(thread_id, "user", user, turn_no)
            store.add_message(thread_id, "assistant", assistant, turn_no)

        pipeline = MemoryPipeline(store, settings=settings)
        batches = pipeline.ingest_pending(thread_id)
        retriever = RetrievalService(store, settings=settings)

        results = []
        for query, keyword, expected_turn in PROBES:
            rows = retriever.retrieve(thread_id, query, k=10, turn_no=10)
            hit = None
            for rank, row in enumerate(rows, start=1):
                content = str(row.get("content") or "")
                if keyword in content:
                    hit = {
                        "rank": rank,
                        "chunk_no": row.get("chunk_no"),
                        "turn_start": row.get("turn_start"),
                        "turn_end": row.get("turn_end"),
                    }
                    break
            results.append(
                {
                    "query": query,
                    "keyword": keyword,
                    "expected_turn": expected_turn,
                    "hit": hit,
                }
            )

        report = {
            "thread_id": thread_id,
            "batches": batches,
            "turns": len(TURNS),
            "probes": results,
            "hit@5": sum(
                1 for item in results if item["hit"] and item["hit"]["rank"] <= 5
            )
            / len(results),
        }
        target = (
            Path(tempfile.gettempdir()) / "mem_ol_eval" / "dialogue_memory_eval.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("report:", target)
    finally:
        store.close()


if __name__ == "__main__":
    main()
