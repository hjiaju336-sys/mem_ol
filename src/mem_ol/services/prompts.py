from __future__ import annotations

import re
from pathlib import Path
from string import Template

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def load_prompt(name: str) -> tuple[str, str]:
    """从 prompts/*.md 读取 System 与 User 两段。"""
    path = _PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")

    system_match = re.search(r"^## System\s*$", text, flags=re.MULTILINE)
    user_match = re.search(r"^## User\s*$", text, flags=re.MULTILINE)
    if not system_match or not user_match or user_match.start() <= system_match.end():
        raise ValueError(f"提示词格式不合法，缺少 ## System / ## User: {path}")

    system = text[system_match.end() : user_match.start()].strip()
    user = text[user_match.end() :].strip()
    return system, user


def render_prompt(template: str, **values: object) -> str:
    """使用 string.Template，避免 JSON 花括号与 format 冲突。"""
    return Template(template).safe_substitute(**values)
