from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_probes import ROUTE_PROBES  # noqa: E402
from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.mcp_server.tools import (  # noqa: E402
    close_tool_service,
    get_tool_service,
)
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.services.llm import get_llm_service  # noqa: E402
from mem_ol.services.pre_turn import PreTurnService, _fuse_entities  # noqa: E402
from mem_ol.services.rerank import get_rerank_service  # noqa: E402


def _content_rank(rows, keywords, mode):
    for rank, row in enumerate(rows, start=1):
        text = str(row.get("content") or "")
        ok = all(word in text for word in keywords) if mode == "all" else any(
            word in text for word in keywords
        )
        if ok:
            return rank
    return None


def _entity_rank(rows, expected):
    for rank, row in enumerate(rows, start=1):
        if row.get("canonical_name") == expected:
            return rank
    return None


def _metrics(ranks):
    n = len(ranks)
    return {
        "n": n,
        "recall@1": sum(1 for r in ranks if r == 1) / n if n else 0.0,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / n if n else 0.0,
        "mrr": sum(1.0 / r for r in ranks if r) / n if n else 0.0,
    }


def main() -> None:
    setup_logging()
    settings = get_settings()
    tools = get_tool_service()
    store = tools.store
    retriever = RetrievalService(store, settings=settings)
    analyzer = PreTurnService(
        tools,
        store=store,
        llm=get_llm_service(),
        settings=settings,
        rerank=object(),
    )
    reranker = get_rerank_service()
    report = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    try:
        thread = store.get_or_create_thread("斗破·60k 评测")
        tid = str(thread["id"])
        context_ranks, keyword_ranks, vector_ranks, union_ranks = [], [], [], []
        any_hits = 0
        rerank_before, rerank_after = [], []
        detail = []

        for query, expected, keywords, mode in ROUTE_PROBES:
            analysis = analyzer.classify(tid, query, turn_no=0)
            rewritten = analysis.rewritten_query or query
            terms = analysis.entities or [query]

            context_rows = retriever.retrieve(tid, rewritten, k=10, turn_no=0)
            context_rank = _content_rank(context_rows, keywords, mode)

            keyword_rows = tools.entity_recall_keyword(
                thread_id=tid, keywords=terms, limit=10
            ).get("candidates", [])
            vector_rows = tools.entity_recall_vector(
                thread_id=tid, query=" ".join(terms), limit=10
            ).get("candidates", [])
            fused_rows = _fuse_entities(keyword_rows, vector_rows)
            keyword_rank = _entity_rank(keyword_rows, expected)
            vector_rank = _entity_rank(vector_rows, expected)
            union_rank = _entity_rank(fused_rows, expected)

            before = _content_rank(context_rows, keywords, mode)
            reranked = reranker.rerank(
                query=rewritten,
                entities=[],
                contexts=context_rows,
                entity_top_k=0,
                context_top_k=10,
            )["contexts"]
            after = _content_rank(reranked, keywords, mode)

            context_ranks.append(context_rank)
            keyword_ranks.append(keyword_rank)
            vector_ranks.append(vector_rank)
            union_ranks.append(union_rank)
            rerank_before.append(before)
            rerank_after.append(after)
            if (context_rank and context_rank <= 5) or (
                union_rank and union_rank <= 5
            ):
                any_hits += 1
            detail.append(
                {
                    "query": query,
                    "expected_entity": expected,
                    "terms": terms,
                    "context_rank": context_rank,
                    "keyword_rank": keyword_rank,
                    "vector_rank": vector_rank,
                    "union_rank": union_rank,
                    "rerank_before": before,
                    "rerank_after": after,
                }
            )

        report["routes"] = {
            "context": _metrics(context_ranks),
            "entity_keyword": _metrics(keyword_ranks),
            "entity_vector": _metrics(vector_ranks),
            "entity_union": _metrics(union_ranks),
            "any_route_hit@5": any_hits / len(ROUTE_PROBES),
            "context_rerank_before": _metrics(rerank_before),
            "context_rerank_after": _metrics(rerank_after),
        }
        report["detail"] = detail
        target = Path(tempfile.gettempdir()) / "mem_ol_eval" / "route_eval.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps(report["routes"], ensure_ascii=False, indent=2))
        print("report:", target)
    finally:
        close_tool_service()


if __name__ == "__main__":
    main()
