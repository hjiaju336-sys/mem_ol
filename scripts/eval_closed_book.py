from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_probes import WORLD_ALIAS, WORLD_FACT  # noqa: E402
from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import setup_logging  # noqa: E402
from mem_ol.services.llm import get_llm_service  # noqa: E402


def _score(answer: str, keywords: list[str], mode: str) -> bool:
    text = answer or ""
    return (
        all(word in text for word in keywords)
        if mode == "all"
        else any(word in text for word in keywords)
    )


def _ask(item):
    query, keywords, mode = item
    llm = get_llm_service()
    answer = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "只凭你的训练记忆回答，不要使用任何外部检索、工具或资料。"
                    "不确定就回答「不知道」。"
                ),
            },
            {"role": "user", "content": query},
        ],
        max_tokens=200,
    )
    answer = answer.strip()
    return {
        "query": query,
        "keywords": keywords,
        "answer": answer,
        "known": _score(answer, keywords, mode),
    }


def main() -> None:
    setup_logging()
    probes = [("fact", item) for item in WORLD_FACT] + [
        ("alias", item) for item in WORLD_ALIAS
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda item: _ask(item[1]), probes))
    for (kind, _), result in zip(probes, results):
        result["kind"] = kind
    known = [item for item in results if item["known"]]
    clean = [item for item in results if not item["known"]]
    report = {
        "n": len(results),
        "closed_book_known": len(known),
        "closed_book_accuracy": len(known) / len(results) if results else 0.0,
        "clean_probes": len(clean),
        "known": known,
        "clean": clean,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    target = Path("C:/Users/23292/AppData/Local/Temp/mem_ol_eval/closed_book.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("report:", target)


if __name__ == "__main__":
    main()
