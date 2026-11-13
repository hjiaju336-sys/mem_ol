from __future__ import annotations

import re
import sys
from pathlib import Path


def clean_wikitext(raw: str) -> str:
    text = re.sub(r"\{\{header.*?\n\}\}\s*", "", raw, flags=re.S | re.I)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.S)
    text = re.sub(r"\[\[[^|\]]*\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"__[A-Z]+__", "", text)

    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(":"):
            line = line[1:].strip()
        if not line or line.startswith("Category:"):
            continue
        lines.append(line)
    return "\n\n".join(lines) + "\n"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_worldview.py <input> <output>")
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    cleaned = clean_wikitext(source.read_text(encoding="utf-8"))
    target.write_text(cleaned, encoding="utf-8")
    print(f"written {target} chars={len(cleaned)}")


if __name__ == "__main__":
    main()
