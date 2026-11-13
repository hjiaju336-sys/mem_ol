from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^\s*第[一二三四五六七八九十百零〇两]+回.*$", re.MULTILINE)


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: extract_gutenberg_chapters.py <input> <output> <chapter_count>"
        )
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    chapter_count = int(sys.argv[3])

    raw = source.read_text(encoding="utf-8")
    headings = list(HEADING.finditer(raw))
    print("headings:", len(headings))
    for match in headings[:8]:
        print(match.start(), match.group(0)[:40])

    if not headings:
        raise SystemExit("no chapter headings found")
    end = (
        headings[chapter_count].start()
        if len(headings) > chapter_count
        else len(raw)
    )
    text = raw[headings[0].start() : end].strip() + "\n"
    target.write_text(text, encoding="utf-8")
    print(f"written {target} chars={len(text)}")


if __name__ == "__main__":
    main()
