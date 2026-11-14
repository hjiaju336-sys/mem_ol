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


def load_system_prompt(name: str) -> str:
    """读取提示词的 System 段；没有分段时返回全文。"""
    path = _PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")
    system_match = re.search(r"^## System\s*$", text, flags=re.MULTILINE)
    if not system_match:
        return text.strip()
    rest = text[system_match.end() :]
    user_match = re.search(r"^## User\s*$", rest, flags=re.MULTILINE)
    return (rest[: user_match.start()] if user_match else rest).strip()


def load_prompt_sections(name: str) -> dict[str, str]:
    """按 `## key` 切分提示词文件，用于模块库。"""
    path = _PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## (.+?)\s*$", text, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1).strip().lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[key] = text[match.end() : end].strip()
    return sections


def render_prompt(template: str, **values: object) -> str:
    """使用 string.Template，避免 JSON 花括号与 format 冲突。"""
    return Template(template).safe_substitute(**values)
