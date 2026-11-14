from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.mcp_server.tools import (  # noqa: E402
    close_tool_service,
    get_tool_service,
)
from mem_ol.pipelines.query import RetrievalService  # noqa: E402
from mem_ol.services.llm import get_llm_service  # noqa: E402
from mem_ol.storage.postgres import PostgresStore  # noqa: E402


class EvalQuestion(BaseModel):
    index: int
    question: str = Field(min_length=1, max_length=200)


class EvalQuestions(BaseModel):
    questions: list[EvalQuestion] = Field(default_factory=list)


def _rank(gold: str, rows: list[dict], key: str) -> int | None:
    for rank, row in enumerate(rows, start=1):
        if str(row.get(key)) == str(gold):
            return rank
    return None


def _parent_no(chunk_no: str) -> str:
    return chunk_no.rsplit("-C", 1)[0]


def _metrics(ranks: list[int | None], k: int = 10) -> dict[str, float]:
    total = len(ranks)
    if not total:
        return {"n": 0, "recall@1": 0.0, "recall@5": 0.0, f"recall@{k}": 0.0, "mrr": 0.0}
    return {
        "n": total,
        "recall@1": sum(1 for r in ranks if r == 1) / total,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / total,
        f"recall@{k}": sum(1 for r in ranks if r and r <= k) / total,
        "mrr": sum(1.0 / r for r in ranks if r) / total,
    }


def _sample_chunks(
    store: PostgresStore, thread_id: str, count: int, seed: int
) -> list[dict]:
    chunks = [
        chunk
        for chunk in store.list_chunks(thread_id)
        if chunk["level"] == "child"
        and 50 <= int(chunk.get("content_length") or 0) <= 120
    ]
    random.Random(seed).shuffle(chunks)
    return chunks[:count]


def _make_questions(llm, chunks: list[dict], timeout: float) -> list[tuple[dict, str]]:
    payload = "\n\n".join(
        f"[{index}] {chunk['content']}" for index, chunk in enumerate(chunks)
    )
    prompt = (
        "下面是从一段中文小说中切出的若干片段，每个片段前有编号。\n"
        "请为每个片段写一个**只能依赖该片段内容回答**的具体问题。\n"
        "要求：\n"
        "1. 问题要具体到片段中的细节，不要问“这段讲了什么”；\n"
        "2. 不要直接复制片段中超过 10 个字的原句；\n"
        "3. 每个片段恰好一个问题；\n"
        "4. 只输出 JSON：{\"questions\":[{\"index\":0,\"question\":\"...\"}]}。\n\n"
        + payload
    )
    result = llm.chat_json_model(
        EvalQuestions,
        [{"role": "system", "content": "你是检索评测问题生成器。"}, {"role": "user", "content": prompt}],
        max_tokens=1600,
        timeout=timeout,
    )
    by_index = {item.index: item.question.strip() for item in result.questions}
    pairs = [(chunk, by_index[index]) for index, chunk in enumerate(chunks) if by_index.get(index)]
    return pairs


def _eval_entities(tools, thread_id: str, entities: list[dict], k: int) -> dict:
    keyword_ranks: list[int | None] = []
    vector_ranks: list[int | None] = []
    union_ranks: list[int | None] = []
    alias_queries = 0
    for entity in entities:
        gold = str(entity["entity_id"])
        aliases = [str(item) for item in (entity.get("aliases") or []) if str(item).strip()]
        query = aliases[0] if aliases else entity["canonical_name"]
        if aliases:
            alias_queries += 1
        keyword_rows = tools.entity_recall_keyword(
            thread_id=thread_id, keywords=[query], limit=k
        ).get("candidates", [])
        vector_rows = tools.entity_recall_vector(
            thread_id=thread_id, query=query, limit=k
        ).get("candidates", [])
        keyword_rank = _rank(gold, keyword_rows, "entity_id")
        vector_rank = _rank(gold, vector_rows, "entity_id")
        keyword_ranks.append(keyword_rank)
        vector_ranks.append(vector_rank)
        ranks = [rank for rank in (keyword_rank, vector_rank) if rank]
        union_ranks.append(min(ranks) if ranks else None)
    return {
        "n": len(entities),
        "alias_queries": alias_queries,
        "keyword": _metrics(keyword_ranks, k),
        "vector": _metrics(vector_ranks, k),
        "union": _metrics(union_ranks, k),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="记忆检索评测：上下文块 + 实体双路")
    parser.add_argument("--thread-name", default="斗破·60k 评测")
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    tools = get_tool_service()
    store = tools.store
    try:
        thread = store.get_or_create_thread(args.thread_name)
        thread_id = str(thread["id"])
        chunks = _sample_chunks(store, thread_id, args.samples, args.seed)
        if not chunks:
            raise SystemExit("没有可评测的子块，请先导入世界文本")

        llm = get_llm_service()
        pairs = _make_questions(llm, chunks, settings.dialogue_timeout)
        retriever = RetrievalService(store, settings=settings)
        context_ranks: list[int | None] = []
        parent_ranks: list[int | None] = []
        detail: list[dict] = []
        for chunk, question in pairs:
            rows = retriever.retrieve(
                thread_id, question, k=args.k, turn_no=0
            )
            rank = _rank(chunk["chunk_no"], rows, "chunk_no")
            gold_parent = _parent_no(chunk["chunk_no"])
            parent_rank = _rank(gold_parent, rows, "parent_no")
            context_ranks.append(rank)
            parent_ranks.append(parent_rank)
            detail.append(
                {
                    "gold_chunk_no": chunk["chunk_no"],
                    "gold_parent_no": gold_parent,
                    "question": question,
                    "rank": rank,
                    "parent_rank": parent_rank,
                }
            )

        entities = store.list_entity_index(thread_id)
        entity_metrics = _eval_entities(tools, thread_id, entities, args.k)
        report = {
            "thread_id": thread_id,
            "thread_name": thread["name"],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "k": args.k,
            "chunks_total": len(store.list_chunks(thread_id)),
            "entities_total": len(entities),
            "context": _metrics(context_ranks, args.k),
            "context_parent": _metrics(parent_ranks, args.k),
            "entity": entity_metrics,
            "details": detail,
        }
        target = Path(args.report) if args.report else (
            Path(tempfile.gettempdir()) / "mem_ol_eval" / "memory_eval.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=2))
        print("report:", target)
    finally:
        close_tool_service()


if __name__ == "__main__":
    main()
