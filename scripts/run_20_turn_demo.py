"""跑 20 轮剧情对话，再发记忆探针，结果写到临时目录。"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import requests

API = "http://127.0.0.1:8000/api"

TURNS = [
    "我叫沈砚，是一名刚到乌坦城的炼药师学徒。",
    "我随身带着一枚青色玉佩，背面刻着一个「衡」字。",
    "我走进萧家后山，想看看斗气修炼的痕迹。",
    "我在后山遇见药老，向他请教筑基灵液。",
    "我答应帮阿蘅找她失踪的哥哥沈川。",
    "三天后，我会在乌坦城东门等阿蘅。",
    "我在坊市买了一些洗骨花。",
    "我把玉佩收进内袋，继续赶路。",
    "我在客栈住下，欠了老板三钱银子。",
    "夜里我听见窗外有人提到三年之约。",
    "第二天清晨，我去了萧家斗技堂。",
    "我在斗技堂看见萧宁在练碎石掌。",
    "我向萧宁打听纳兰嫣然退婚的事。",
    "我回到客栈，把铜钥匙藏在床板下面。",
    "阿蘅托人送来一封信，说东门之约不变。",
    "我去后山采紫叶兰草。",
    "我把采到的紫叶兰草交给药老。",
    "药老告诉我，筑基灵液需要洗骨花、紫叶兰草和魔核。",
    "第五天，我在东门等了很久，阿蘅没有出现。",
    "我拿出青色玉佩，回想这几天发生的事。",
]

PROBES = [
    ("我的玉佩背面刻着什么字？", ["衡"]),
    ("阿蘅的哥哥叫什么名字？", ["沈川"]),
    ("我和阿蘅约在什么时间、哪里见面？", ["三天", "东门"]),
    ("我欠了客栈老板多少钱？", ["三钱"]),
    ("铜钥匙藏在哪里？", ["床板"]),
    ("筑基灵液需要哪些材料？", ["洗骨花", "紫叶兰草", "魔核"]),
    ("纳兰嫣然退婚事件是怎么回事？", ["退婚", "纳兰嫣然"]),
    ("药老和萧炎是什么关系？", ["师徒", "收徒", "老师", "师父"]),
    ("我第一天说我是谁？", ["沈砚"]),
    ("从东门之约到现在大概过了多久？", ["天"]),
]

CUSTOM = (
    "玩家扮演沈砚，一个刚到乌坦城的炼药师学徒。"
    "不要替玩家扮演萧炎或其他既有角色；只写玩家周围的世界与人物反应。"
)


def wait_batches(thread_id: str, expected: int, timeout: float = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        batches = requests.get(
            f"{API}/threads/{thread_id}/batches", timeout=30
        ).json()
        done = [
            b
            for b in batches
            if b.get("kind") == "dialogue" and b.get("status") == "done"
        ]
        if len(done) >= expected:
            print(f"  [ingest] done dialogue batches={len(done)}", flush=True)
            return
        time.sleep(3)
    print("  [ingest] wait timeout", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-name", default="斗破·60k·glm")
    parser.add_argument("--turns", type=int, default=20)
    args = parser.parse_args()

    threads = requests.get(f"{API}/threads", timeout=30).json()
    thread = next((t for t in threads if t["name"] == args.thread_name), None)
    if thread is None:
        thread = requests.post(
            f"{API}/threads", json={"name": args.thread_name}, timeout=30
        ).json()
    tid = thread["id"]
    print("thread", tid, flush=True)

    results = []
    for index, text in enumerate(TURNS[: args.turns], start=1):
        started = time.time()
        response = requests.post(
            f"{API}/threads/{tid}/messages",
            json={
                "content": text,
                "client_context": {"custom_prompt": CUSTOM},
            },
            timeout=330,
        )
        if response.status_code != 200:
            print("FAIL", index, response.status_code, response.text[:300], flush=True)
            break
        data = response.json()
        print(
            f"[turn {index}] chars={len(data.get('reply') or '')} "
            f"intent={data.get('intent')} time={round(time.time()-started,1)}s",
            flush=True,
        )
        results.append(
            {
                "turn": index,
                "input": text,
                "reply": data.get("reply"),
                "intent": data.get("intent"),
                "trace_id": data.get("trace_id"),
                "state_view": data.get("state_view"),
            }
        )
        if index % 5 == 0:
            wait_batches(tid, index // 5)

    print("waiting for plot maintenance...", flush=True)
    time.sleep(10)

    probes = []
    for query, keywords in PROBES:
        response = requests.post(
            f"{API}/threads/{tid}/messages",
            json={
                "content": query,
                "client_context": {"custom_prompt": CUSTOM},
            },
            timeout=330,
        )
        if response.status_code != 200:
            probes.append(
                {"query": query, "error": response.text[:300]}
            )
            continue
        data = response.json()
        reply = data.get("reply") or ""
        hit = all(word in reply for word in keywords) if len(keywords) > 1 else any(
            word in reply for word in keywords
        )
        probes.append(
            {
                "query": query,
                "keywords": keywords,
                "reply": reply,
                "hit": hit,
                "trace_id": data.get("trace_id"),
                "state_view": data.get("state_view"),
            }
        )
        print(f"[probe] hit={hit} {query[:30]}", flush=True)

    traces = requests.get(f"{API}/threads/{tid}/traces", timeout=30).json()
    report = {
        "thread_id": tid,
        "turns": results,
        "probes": probes,
        "traces": traces,
    }
    target = Path(tempfile.gettempdir()) / "mem_ol_eval" / "glm_20turn.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("report:", target, flush=True)


if __name__ == "__main__":
    main()
