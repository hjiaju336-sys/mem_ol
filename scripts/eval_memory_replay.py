"""复用上一轮 LLM 生成的问题，重跑上下文召回，不调用 LLM。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.storage.postgres import PostgresStore  # noqa: E402


def _rank(gold, rows, key):
    for rank, row in enumerate(rows, start=1):
        if str(row.get(key)) == str(gold):
            return rank
    return None


def _metrics(ranks):
    n = len(ranks)
    return {
        "n": n,
        "recall@1": sum(1 for r in ranks if r == 1) / n if n else 0.0,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / n if n else 0.0,
        "recall@10": sum(1 for r in ranks if r and r <= 10) / n if n else 0.0,
        "mrr": sum(1.0 / r for r in ranks if r) / n if n else 0.0,
    }


def main() -> None:
    setup_logging()
    settings = get_settings()
    old_path = Path(tempfile.gettempdir()) / "mem_ol_eval" / "memory_eval_24.json"
    old = json.loads(old_path.read_text(encoding="utf-8"))
    store = PostgresStore(settings.postgres_dsn)
    try:
        thread = store.get_or_create_thread("斗破·60k 评测-v2")
        tid = str(thread["id"])
        retriever = RetrievalService(store, settings=settings)
        child_ranks, parent_ranks = [], []
        detail = []
        for item in old.get("details", []):
            gold = item["gold_chunk_no"]
            gold_parent = gold.rsplit("-C", 1)[0]
            rows = retriever.retrieve(tid, item["question"], k=10, turn_no=0)
            child_rank = _rank(gold, rows, "chunk_no")
            parent_rank = _rank(gold_parent, rows, "parent_no")
            child_ranks.append(child_rank)
            parent_ranks.append(parent_rank)
            detail.append(
                {
                    "question": item["question"],
                    "gold_chunk_no": gold,
                    "child_rank": child_rank,
                    "parent_rank": parent_rank,
                }
            )
        report = {
            "thread_id": tid,
            "context": _metrics(child_ranks),
            "context_parent": _metrics(parent_ranks),
            "detail": detail,
        }
        target = Path(tempfile.gettempdir()) / "mem_ol_eval" / "memory_eval_v2.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps({k: v for k, v in report.items() if k != "detail"}, ensure_ascii=False, indent=2))
        print("report:", target)
    finally:
        store.close()


if __name__ == "__main__":
    main()
