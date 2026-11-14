from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.domain.rules import get_rules  # noqa: E402
from mem_ol.services.prompts import (  # noqa: E402
    load_prompt,
    load_prompt_sections,
    load_system_prompt,
)

PROMPTS = ROOT / "prompts"
SECTION_PROMPTS = {"gm_contracts.md"}


def main() -> None:
    rules = get_rules()
    print(
        "规则包:",
        f"player.hp={rules.state.player.get('hp')}",
        f"dc_base={rules.attack.dc_base}",
        f"max_effects={rules.effects.max_per_turn}",
    )

    failures: list[str] = []
    for path in sorted(PROMPTS.glob("*.md")):
        try:
            if path.name in SECTION_PROMPTS:
                sections = load_prompt_sections(path.name)
                if not sections:
                    failures.append(f"{path.name}: 没有 ## section")
                else:
                    print(f"{path.name}: {len(sections)} sections")
                continue
            text = path.read_text(encoding="utf-8")
            if "## User" in text:
                load_prompt(path.name)
                print(f"{path.name}: System/User ok")
            else:
                load_system_prompt(path.name)
                print(f"{path.name}: System ok")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: {exc}")

    if failures:
        print("校验失败:")
        for item in failures:
            print(" -", item)
        raise SystemExit(1)
    print("提示词与规则包校验通过")


if __name__ == "__main__":
    main()
