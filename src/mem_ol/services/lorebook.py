from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from mem_ol.log import get_logger

logger = get_logger(__name__)

SELECTIVE_AND_ANY = 0
SELECTIVE_NOT_ALL = 1
SELECTIVE_NOT_ANY = 2
SELECTIVE_AND_ALL = 3

_ASCII_WORD = re.compile(r"^[A-Za-z0-9_]+$")
_REGEX_KEY = re.compile(r"^/(.+)/([a-zA-Z]*)$", re.DOTALL)
_MACRO_USER = re.compile(r"\{\{\s*user\s*\}\}", re.IGNORECASE)
_MACRO_CHAR = re.compile(r"\{\{\s*char\s*\}\}", re.IGNORECASE)


@dataclass
class LorebookEntry:
    uid: str
    keys: list[str]
    secondary_keys: list[str]
    content: str
    enabled: bool = True
    constant: bool = False
    selective: bool = False
    selective_logic: int = SELECTIVE_AND_ANY
    order: int = 100
    position: str = "before_char"
    case_sensitive: bool = False
    match_whole_words: bool = False
    probability: int = 100
    use_probability: bool = False
    scan_depth: int | None = None
    group: str = ""
    prevent_recursion: bool = False
    exclude_recursion: bool = False
    delay_until_recursion: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Lorebook:
    name: str = ""
    source_format: str = "sillytavern"
    entries: list[LorebookEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        payload = json.dumps(self.raw, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _unwrap(raw: Any) -> tuple[Any, str]:
    """支持独立世界书、character_book、角色卡外层。"""
    if not isinstance(raw, dict):
        raise ValueError("世界书必须是 JSON 对象")
    if "entries" in raw:
        entries = raw["entries"]
        return raw, "sillytavern" if isinstance(entries, dict) else "character_book_v2"
    if isinstance(raw.get("character_book"), dict):
        return raw["character_book"], "character_book_v2"
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("character_book"), dict):
        return data["character_book"], "character_book_v2"
    raise ValueError("世界书缺少 entries")


def _position_from_native(value: Any) -> str:
    # SillyTavern 原生 position：0=before_char，1=after_char，其余位置暂映射到 after。
    if _as_int(value, 1) == 0:
        return "before_char"
    return "after_char"


def _position_from_v2(value: Any) -> str:
    return "before_char" if str(value or "").strip() == "before_char" else "after_char"


def _entry_from_native(uid: str, item: dict[str, Any]) -> LorebookEntry:
    return LorebookEntry(
        uid=str(item.get("uid", uid)),
        keys=_as_list(item.get("key")),
        secondary_keys=_as_list(item.get("keysecondary")),
        content=str(item.get("content") or ""),
        enabled=not _as_bool(item.get("disable"), False),
        constant=_as_bool(item.get("constant"), False),
        selective=_as_bool(item.get("selective"), False),
        selective_logic=_as_int(item.get("selectiveLogic"), SELECTIVE_AND_ANY),
        order=_as_int(item.get("order"), 100),
        position=_position_from_native(item.get("position")),
        case_sensitive=_as_bool(item.get("caseSensitive"), False),
        match_whole_words=_as_bool(item.get("matchWholeWords"), False),
        probability=_as_int(item.get("probability"), 100),
        use_probability=_as_bool(item.get("useProbability"), False),
        scan_depth=(
            _as_int(item.get("scanDepth"), 0) or None
            if item.get("scanDepth") is not None
            else None
        ),
        group=str(item.get("group") or ""),
        prevent_recursion=_as_bool(item.get("preventRecursion"), False),
        exclude_recursion=_as_bool(item.get("excludeRecursion"), False),
        delay_until_recursion=_as_bool(item.get("delayUntilRecursion"), False),
        raw=dict(item),
    )


def _entry_from_v2(index: int, item: dict[str, Any]) -> LorebookEntry:
    extensions = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}
    return LorebookEntry(
        uid=str(item.get("id", index)),
        keys=_as_list(item.get("keys")),
        secondary_keys=_as_list(item.get("secondary_keys")),
        content=str(item.get("content") or ""),
        enabled=_as_bool(item.get("enabled"), True),
        constant=_as_bool(item.get("constant"), False),
        selective=_as_bool(item.get("selective"), False),
        selective_logic=SELECTIVE_AND_ANY,
        order=_as_int(item.get("insertion_order"), 100),
        position=_position_from_v2(item.get("position")),
        case_sensitive=_as_bool(item.get("case_sensitive"), False),
        group=str(extensions.get("group") or item.get("group") or ""),
        raw=dict(item),
    )


def parse_lorebook(raw: Any) -> Lorebook:
    """解析 SillyTavern 原生世界书或 Character Card V2 character_book。"""
    book_raw, source_format = _unwrap(raw)
    entries_raw = book_raw.get("entries")
    entries: list[LorebookEntry] = []
    if isinstance(entries_raw, dict):
        for uid, item in entries_raw.items():
            if isinstance(item, dict):
                entries.append(_entry_from_native(str(uid), item))
    elif isinstance(entries_raw, list):
        for index, item in enumerate(entries_raw):
            if isinstance(item, dict):
                entries.append(_entry_from_v2(index, item))
    else:
        raise ValueError("世界书 entries 必须是对象或数组")
    return Lorebook(
        name=str(book_raw.get("name") or ""),
        source_format=source_format,
        entries=entries,
        raw=dict(book_raw),
    )


def _match_key(
    text: str, key: str, case_sensitive: bool, match_whole_words: bool
) -> bool:
    if not key:
        return False
    regex_match = _REGEX_KEY.match(key)
    if regex_match and len(key) <= 200:
        pattern, flags = regex_match.group(1), regex_match.group(2)
        try:
            return re.search(
                pattern,
                text,
                re.IGNORECASE if "i" in flags or not case_sensitive else 0,
            ) is not None
        except re.error:
            logger.warning(
                "世界书正则 key 非法，按普通文本匹配",
                extra={"event": "lorebook.key.regex_invalid", "key": key[:80]},
            )
    haystack = text if case_sensitive else text.lower()
    needle = key if case_sensitive else key.lower()
    if match_whole_words and _ASCII_WORD.match(key):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])",
            haystack,
        ) is not None
    return needle in haystack


def _selective_ok(entry: LorebookEntry, text: str) -> bool:
    if not entry.selective or not entry.secondary_keys:
        return True
    matched = [
        key
        for key in entry.secondary_keys
        if _match_key(text, key, entry.case_sensitive, entry.match_whole_words)
    ]
    if entry.selective_logic == SELECTIVE_NOT_ALL:
        return len(matched) < len(entry.secondary_keys)
    if entry.selective_logic == SELECTIVE_NOT_ANY:
        return not matched
    if entry.selective_logic == SELECTIVE_AND_ALL:
        return len(matched) == len(entry.secondary_keys)
    return bool(matched)


def _entry_matches(entry: LorebookEntry, text: str, rng: random.Random) -> bool:
    if not entry.enabled or not entry.content.strip():
        return False
    if entry.constant:
        return True
    if not any(
        _match_key(text, key, entry.case_sensitive, entry.match_whole_words)
        for key in entry.keys
    ):
        return False
    if not _selective_ok(entry, text):
        return False
    if entry.use_probability:
        probability = max(0, min(100, entry.probability))
        if rng.randint(1, 100) > probability:
            return False
    return True


def _scan_text_for(
    entry: LorebookEntry,
    default_text: str,
    recent_messages: Sequence[str] | None,
    default_scan_depth: int,
) -> str:
    depth = entry.scan_depth or default_scan_depth
    if recent_messages is None or depth <= 0:
        return default_text
    selected = list(recent_messages)[-depth:]
    return "\n".join(selected) + "\n" + default_text


def _apply_macros(text: str) -> str:
    return _MACRO_CHAR.sub("GM", _MACRO_USER.sub("玩家", text))


def select_entries(
    book: Lorebook,
    text: str,
    *,
    recent_messages: Sequence[str] | None = None,
    scan_depth: int = 3,
    max_chars: int = 1500,
    recursion_steps: int = 0,
    rng: random.Random | None = None,
) -> list[LorebookEntry]:
    """按关键词/常驻/概率选出本轮条目，并按字符预算裁剪。"""
    rng = rng or random.Random()
    base_text = text
    candidates: dict[str, LorebookEntry] = {}

    for entry in book.entries:
        if entry.delay_until_recursion:
            continue
        scan_text = _scan_text_for(entry, base_text, recent_messages, scan_depth)
        if _entry_matches(entry, scan_text, rng):
            candidates[entry.uid] = entry

    if recursion_steps > 0:
        for step in range(1, recursion_steps + 1):
            new_entries: dict[str, LorebookEntry] = {}
            for source_uid, source in list(candidates.items()):
                if source.prevent_recursion:
                    continue
                scan_text = _apply_macros(source.content)
                for entry in book.entries:
                    if entry.uid in candidates or entry.uid in new_entries:
                        continue
                    if entry.exclude_recursion:
                        continue
                    if entry.delay_until_recursion and step < 1:
                        continue
                    if _entry_matches(
                        entry,
                        _scan_text_for(
                            entry, scan_text, recent_messages, scan_depth
                        ),
                        rng,
                    ):
                        new_entries[entry.uid] = entry
            if not new_entries:
                break
            candidates.update(new_entries)

    ordered = sorted(
        candidates.values(),
        key=lambda item: (item.constant, item.order),
        reverse=True,
    )
    selected: list[LorebookEntry] = []
    used = 0
    for entry in ordered:
        content = _apply_macros(entry.content)
        if len(content) > max_chars:
            logger.warning(
                "世界书单条内容超过预算，已跳过",
                extra={
                    "event": "lorebook.entry.too_large",
                    "uid": entry.uid,
                    "chars": len(content),
                    "budget": max_chars,
                },
            )
            continue
        if used + len(content) > max_chars:
            continue
        selected.append(entry)
        used += len(content)
        if used >= max_chars:
            break
    return selected


def render_lorebook(entries: Iterable[LorebookEntry]) -> tuple[str, str]:
    """返回 (before_char 文本, after_char 文本)。"""
    buckets: dict[str, list[LorebookEntry]] = {
        "before_char": [],
        "after_char": [],
    }
    for entry in entries:
        bucket = entry.position if entry.position in buckets else "after_char"
        buckets[bucket].append(entry)

    def render(items: list[LorebookEntry]) -> str:
        ordered = sorted(items, key=lambda item: (not item.constant, item.order))
        blocks = []
        for entry in ordered:
            label = entry.raw.get("comment") or "、".join(entry.keys[:3]) or entry.uid
            blocks.append(f"[{label}]\n{_apply_macros(entry.content)}")
        return "\n\n".join(blocks)

    return render(buckets["before_char"]), render(buckets["after_char"])


def active_uids(entries: Iterable[LorebookEntry]) -> list[str]:
    return [entry.uid for entry in entries]
