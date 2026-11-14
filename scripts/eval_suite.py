from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.mcp_server.tools import (  # noqa: E402
    close_tool_service,
    get_tool_service,
)
from mem_ol.pipelines.memory import MemoryPipeline  # noqa: E402
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.services.llm import get_llm_service  # noqa: E402
from mem_ol.services.pre_turn import PreTurnService  # noqa: E402

from eval_probes import (  # noqa: E402
    DIALOGUE_PROBES,
    RELATIONS,
    TURNS,
    WORLD_ALIAS,
    WORLD_FACT,
)


def _rank(rows, keywords, mode="any", key="content"):
    for rank, row in enumerate(rows, start=1):
        content = str(row.get(key) or "")
        ok = (
            all(word in content for word in keywords)
            if mode == "all"
            else any(word in content for word in keywords)
        )
        if ok:
            return rank
    return None


def _metrics(ranks, k=10):
    n = len(ranks)
    if not n:
        return {"n": 0, "recall@1": 0.0, "recall@5": 0.0, f"recall@{k}": 0.0, "mrr": 0.0}
    return {
        "n": n,
        "recall@1": sum(1 for r in ranks if r == 1) / n,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / n,
        f"recall@{k}": sum(1 for r in ranks if r and r <= k) / n,
        "mrr": sum(1.0 / r for r in ranks if r) / n,
    }


def _run_world(store, retriever, thread_id, k):
    def run(probes):
        ranks = []
        detail = []
        for query, keywords, mode in probes:
            rows = retriever.retrieve(thread_id, query, k=k, turn_no=0)
            rank = _rank(rows, keywords, mode)
            ranks.append(rank)
            detail.append({"query": query, "keywords": keywords, "rank": rank})
        return _metrics(ranks, k), detail

    fact, fact_detail = run(WORLD_FACT)
    alias, alias_detail = run(WORLD_ALIAS)
    return {"fact": fact, "alias": alias}, fact_detail + alias_detail


def _run_relations(tools, thread_id):
    found = 0
    detail = []
    for source, target, labels in RELATIONS:
        candidates = tools.entity_recall_keyword(
            thread_id=thread_id, keywords=[source], limit=5
        ).get("candidates", [])
        entity_id = next(
            (
                item["entity_id"]
                for item in candidates
                if item.get("canonical_name") == source
            ),
            None,
        )
        hit = False
        if entity_id:
            data = tools.entity_get(thread_id=thread_id, entity_id=entity_id)
            for relation in data.get("relations") or []:
                label = str(relation.get("relation_label") or "")
                if (
                    relation.get("source_name") == source
                    and relation.get("target_name") == target
                    and any(word in label for word in labels)
                ):
                    hit = True
                    break
        found += int(hit)
        detail.append(
            {
                "source": source,
                "target": target,
                "relation": labels,
                "hit": hit,
            }
        )
    return {
        "n": len(RELATIONS),
        "coverage": found / len(RELATIONS),
        "detail": detail,
    }


def _run_dialogue(store, tools, llm, settings):
    thread = store.get_or_create_thread("对话记忆-指代/时间/窗口外")
    thread_id = str(thread["id"])
    for turn_no, (user, assistant) in enumerate(TURNS, start=1):
        store.add_message(thread_id, "user", user, turn_no)
        store.add_message(thread_id, "assistant", assistant, turn_no)

    pipeline = MemoryPipeline(store, settings=settings)
    batches = pipeline.ingest_pending(thread_id)
    retriever = RetrievalService(store, settings=settings)
    analyzer = PreTurnService(
        tools,
        store=store,
        llm=llm,
        settings=settings,
        rerank=object(),  # classify 不需要精排，避免加载重模型
    )

    detail = []
    ranks = []
    coref_ok = 0
    for query, keywords, kind in DIALOGUE_PROBES:
        analysis = analyzer.classify(thread_id, query, turn_no=10)
        rewritten = analysis.rewritten_query or query
        rows = retriever.retrieve(thread_id, rewritten, k=10, turn_no=10)
        rank = _rank(rows, keywords, "any")
        ranks.append(rank)
        if kind == "coref":
            coref_ok += int("青锋" in rewritten)
        detail.append(
            {
                "query": query,
                "kind": kind,
                "rewritten_query": rewritten,
                "keywords": keywords,
                "rank": rank,
            }
        )
    return {
        "thread_id": thread_id,
        "turns": len(TURNS),
        "batches": batches,
        "coref_rewrite_ok": coref_ok,
        "retrieval": _metrics(ranks, 10),
        "detail": detail,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="分类型记忆评测")
    parser.add_argument("--thread-name", default="斗破·60k 评测-v2")
    parser.add_argument("--skip-dialogue", action="store_true")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    tools = get_tool_service()
    store = tools.store
    try:
        thread = store.get_or_create_thread(args.thread_name)
        thread_id = str(thread["id"])
        retriever = RetrievalService(store, settings=settings)
        world, world_detail = _run_world(store, retriever, thread_id, 10)
        relation = _run_relations(tools, thread_id)
        dialogue = (
            {"skipped": True}
            if args.skip_dialogue
            else _run_dialogue(store, tools, get_llm_service(), settings)
        )
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "world_thread_id": thread_id,
            "world": world,
            "relation": relation,
            "dialogue": dialogue,
            "world_detail": world_detail,
        }
        target = Path(tempfile.gettempdir()) / "mem_ol_eval" / "suite_eval.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps({k: v for k, v in report.items() if k != "world_detail"}, ensure_ascii=False, indent=2, default=str))
        print("report:", target)
    finally:
        close_tool_service()


if __name__ == "__main__":
    main()
