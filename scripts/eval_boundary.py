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
from mem_ol.services.rollback import RollbackService  # noqa: E402
from mem_ol.storage.neo4j import Neo4jStore  # noqa: E402

LONG_TURNS = [
    ("我的佩剑叫青锋，剑柄上刻着一只白鹤。", "记住了。"),
    ("我欠了客栈老板三钱银子。", "记下了。"),
    ("我把铜钥匙藏在床板下面。", "嗯。"),
    ("阿蘅的哥哥叫沈川。", "知道了。"),
    ("我的左臂有一道旧伤。", "明白。"),
    ("我喝了口水。", "你休息了一会儿。"),
    ("窗外开始下雨。", "雨声渐密。"),
    ("我检查了门闩。", "门闩完好。"),
    ("我坐在床边。", "夜色更深。"),
    ("我吹灭了灯。", "房间暗下来。"),
    ("我听见远处钟声。", "钟声敲了三下。"),
    ("我闭上眼睛。", "你沉沉睡去。"),
]

LONG_PROBES = [
    ("我的佩剑叫什么？", "青锋"),
    ("我欠了多少钱？", "三钱"),
    ("铜钥匙藏在哪里？", "床板"),
    ("阿蘅的哥哥叫什么？", "沈川"),
    ("我的左臂有什么？", "旧伤"),
]

ABSENT_PROBES = [
    "萧炎在迦南学院的宿舍号是多少？",
    "萧炎的孩子叫什么名字？",
    "美杜莎女王的本名是什么？",
    "迦南学院的院长是谁？",
    "萧炎最终成为什么级别的强者？",
]


def _rank(rows, keyword, key="content"):
    for rank, row in enumerate(rows, start=1):
        if keyword in str(row.get(key) or ""):
            return rank
    return None


def _add_turns(store, thread_id, turns):
    for turn_no, (user, assistant) in enumerate(turns, start=1):
        store.add_message(thread_id, "user", user, turn_no)
        store.add_message(thread_id, "assistant", assistant, turn_no)


def _cleanup(store, thread_id):
    with store.pool.connection() as conn:
        for table in (
            "entity_index_jobs",
            "tool_call_logs",
            "checkpoint_writes",
            "checkpoint_blobs",
            "checkpoints",
            "state_events",
            "game_states",
        ):
            try:
                conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", (str(thread_id),))
            except Exception:
                conn.rollback()
        conn.execute("DELETE FROM threads WHERE id=%s", (str(thread_id),))
        conn.commit()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="召回边界评测")
    parser.add_argument("--thread-name", default="斗破·60k 评测-v2")
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    tools = get_tool_service()
    store = tools.store
    neo4j = Neo4jStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    retriever = RetrievalService(store, settings=settings)
    analyzer = PreTurnService(
        tools,
        store=store,
        llm=get_llm_service(),
        settings=settings,
        rerank=object(),
    )
    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    try:
        # 1. 长程 / 窗口外召回
        thread = store.get_or_create_thread("边界-长程")
        tid = str(thread["id"])
        _add_turns(store, tid, LONG_TURNS)
        MemoryPipeline(store, settings=settings).ingest_pending(tid)
        long_ranks = []
        long_detail = []
        positive_scores = []
        for query, keyword in LONG_PROBES:
            rows = retriever.retrieve(tid, query, k=10, turn_no=13)
            rank = _rank(rows, keyword)
            long_ranks.append(rank)
            if rows:
                positive_scores.append(float(rows[0].get("score", 0.0)))
            long_detail.append({"query": query, "keyword": keyword, "rank": rank})
        report["long_range"] = {
            "n": len(long_ranks),
            "recall@1": sum(1 for r in long_ranks if r == 1) / len(long_ranks),
            "recall@5": sum(1 for r in long_ranks if r and r <= 5) / len(long_ranks),
            "detail": long_detail,
        }
        _cleanup(store, tid)

        # 2. 指代：明确 / 歧义
        if args.skip_llm:
            report["coreference"] = {"skipped": True}
            report["time_reference"] = {"skipped": True}
        else:
            clear = store.get_or_create_thread("边界-指代-明确")
            clear_tid = str(clear["id"])
            _add_turns(
                store,
                clear_tid,
                [
                    ("我的佩剑叫青锋。", "记住了。"),
                    ("我把青锋剑放在桌上。", "剑鞘映着灯光。"),
                ],
            )
            clear_analysis = analyzer.classify(clear_tid, "它叫什么名字？", turn_no=3)
            _cleanup(store, clear_tid)

            ambiguous = store.get_or_create_thread("边界-指代-歧义")
            amb_tid = str(ambiguous["id"])
            _add_turns(
                store,
                amb_tid,
                [
                    ("我的佩剑叫青锋。", "记住了。"),
                    ("我把青铜钥匙放在桌上。", "钥匙在桌上。"),
                    ("我把青锋剑和青铜钥匙都拿了起来。", "两样东西都在你手里。"),
                ],
            )
            amb_analysis = analyzer.classify(amb_tid, "它在哪里？", turn_no=4)
            _cleanup(store, amb_tid)

            report["coreference"] = {
                "clear_rewrite": clear_analysis.rewritten_query,
                "clear_ok": "青锋" in clear_analysis.rewritten_query,
                "ambiguous_rewrite": amb_analysis.rewritten_query,
                "ambiguous_mentions": {
                    "sword": "青锋" in amb_analysis.rewritten_query,
                    "key": "钥匙" in amb_analysis.rewritten_query,
                },
            }

            # 3. 时间指代
            time_thread = store.get_or_create_thread("边界-时间")
            time_tid = str(time_thread["id"])
            _add_turns(
                store,
                time_tid,
                [
                    ("我的佩剑叫青锋。", "记住了。"),
                    ("我在客栈欠了三钱银子。", "记下了。"),
                    ("三天前，我离开了乌坦城。", "三天前的事。"),
                    ("今天天气不错。", "是的。"),
                    ("我在房间休息。", "夜色渐深。"),
                    ("窗外有雨。", "雨声渐密。"),
                ],
            )
            time_analysis = analyzer.classify(time_tid, "那件事过去几天了？", turn_no=7)
            _cleanup(store, time_tid)
            report["time_reference"] = {
                "rewrite": time_analysis.rewritten_query,
                "mentions_days": "天" in time_analysis.rewritten_query,
            }

        # 4. 不存在事实：分数边界（同一 60k 世界内比较正/负问题）
        world = store.get_or_create_thread(args.thread_name)
        world_tid = str(world["id"])
        world_positive = [
            "萧炎的未婚妻是谁？",
            "云岚宗位于哪个帝国？",
            "黑色古朴戒指的别名是什么？",
            "丹王古河指的是谁？",
            "八极崩是谁教给萧炎的？",
        ]
        world_positive_scores = []
        for query in world_positive:
            rows = retriever.retrieve(world_tid, query, k=10, turn_no=14)
            if rows:
                world_positive_scores.append(float(rows[0].get("score", 0.0)))
        absent_scores = []
        absent_detail = []
        for query in ABSENT_PROBES:
            rows = retriever.retrieve(world_tid, query, k=10, turn_no=14)
            score = float(rows[0].get("score", 0.0)) if rows else 0.0
            absent_scores.append(score)
            absent_detail.append({"query": query, "top_score": score})
        report["absent"] = {
            "n": len(absent_scores),
            "mean_top_score": sum(absent_scores) / len(absent_scores),
            "positive_mean_top_score": (
                sum(world_positive_scores) / len(world_positive_scores)
                if world_positive_scores
                else 0.0
            ),
            "detail": absent_detail,
        }

        # 5. 回退后召回边界
        rb = store.get_or_create_thread("边界-回退")
        rb_tid = str(rb["id"])
        _add_turns(
            store,
            rb_tid,
            [
                ("我的佩剑叫青锋。", "记住了。"),
                ("我欠了客栈三钱银子。", "记下了。"),
                ("我把铜钥匙藏在床板下。", "嗯。"),
                ("阿蘅的哥哥叫沈川。", "知道了。"),
                ("我在房间休息。", "夜色渐深。"),
            ],
        )
        MemoryPipeline(store, settings=settings).ingest_pending(rb_tid)
        before_rows = retriever.retrieve(rb_tid, "阿蘅的哥哥叫什么？", k=10, turn_no=6)
        before_rank = _rank(before_rows, "沈川")
        RollbackService(store, neo4j).rollback(rb_tid, 2)
        after_rows = retriever.retrieve(rb_tid, "阿蘅的哥哥叫什么？", k=10, turn_no=6)
        after_rank = _rank(after_rows, "沈川")
        remain_rows = retriever.retrieve(rb_tid, "我的佩剑叫什么？", k=10, turn_no=6)
        remain_rank = _rank(remain_rows, "青锋")
        report["rollback"] = {
            "before_rank": before_rank,
            "after_rank": after_rank,
            "remain_rank": remain_rank,
            "deleted_fact_gone": before_rank is not None and after_rank is None,
            "earlier_fact_remains": remain_rank is not None,
        }
        _cleanup(store, rb_tid)

        target = Path(tempfile.gettempdir()) / "mem_ol_eval" / "boundary_eval.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("report:", target)
    finally:
        neo4j.close()
        close_tool_service()


if __name__ == "__main__":
    main()
