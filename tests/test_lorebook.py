import random

from mem_ol.services.lorebook import (
    active_uids,
    parse_lorebook,
    render_lorebook,
    select_entries,
)


def native_book(entries):
    return {"entries": entries}


def test_parse_sillytavern_native_and_select_constant():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["水帘洞"],
                    "content": "水帘洞在花果山深处。",
                    "constant": True,
                    "order": 10,
                },
                "1": {
                    "uid": 1,
                    "key": ["铁扇"],
                    "content": "铁扇公主住在翠云山。",
                    "order": 20,
                },
            }
        )
    )
    assert book.source_format == "sillytavern"
    assert len(book.entries) == 2
    selected = select_entries(book, "我走进山洞", max_chars=1000)
    assert active_uids(selected) == ["0"]


def test_parse_character_book_v2_and_keyword():
    book = parse_lorebook(
        {
            "name": "测试书",
            "entries": [
                {
                    "id": 7,
                    "keys": ["孙悟空"],
                    "content": "孙悟空是花果山的美猴王。",
                    "enabled": True,
                    "insertion_order": 100,
                    "position": "before_char",
                }
            ],
        }
    )
    assert book.source_format == "character_book_v2"
    selected = select_entries(book, "孙悟空跳上树枝", max_chars=1000)
    assert active_uids(selected) == ["7"]


def test_disabled_and_missing_keyword_are_skipped():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["花果山"],
                    "content": "花果山。",
                    "disable": True,
                },
                "1": {"uid": 1, "key": ["天庭"], "content": "天庭。"},
            }
        )
    )
    assert select_entries(book, "花果山", max_chars=1000) == []


def test_selective_and_all_logic():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["水帘洞"],
                    "keysecondary": ["美猴王", "猴群"],
                    "content": "水帘洞的猴群。",
                    "selective": True,
                    "selectiveLogic": 3,
                }
            }
        )
    )
    assert select_entries(book, "水帘洞 美猴王", max_chars=1000) == []
    selected = select_entries(book, "水帘洞 美猴王 猴群", max_chars=1000)
    assert active_uids(selected) == ["0"]


def test_budget_keeps_higher_order_and_constant():
    book = parse_lorebook(
        native_book(
            {
                "0": {"uid": "low", "key": ["山"], "content": "低" * 60, "order": 1},
                "1": {"uid": "high", "key": ["山"], "content": "高" * 60, "order": 100},
                "2": {
                    "uid": "const",
                    "key": [],
                    "content": "常" * 20,
                    "constant": True,
                    "order": 1,
                },
            }
        )
    )
    selected = select_entries(book, "山", max_chars=130)
    uids = active_uids(selected)
    assert "const" in uids
    assert "high" in uids
    assert "low" not in uids


def test_render_position_buckets_and_macros():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["门"],
                    "content": "{{user}}看到{{char}}留下的门。",
                    "constant": True,
                    "position": 0,
                },
                "1": {
                    "uid": 1,
                    "key": ["窗"],
                    "content": "窗外有雨。",
                    "constant": True,
                    "position": 1,
                },
            }
        )
    )
    selected = select_entries(book, "门", max_chars=1000)
    before, after = render_lorebook(selected)
    assert "玩家看到GM留下的门" in before
    assert "窗外有雨" in after


def test_probability_can_drop_entry():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["雨"],
                    "content": "下雨了。",
                    "useProbability": True,
                    "probability": 0,
                }
            }
        )
    )
    assert select_entries(book, "雨", rng=random.Random(1)) == []


def test_recursion_triggers_second_entry():
    book = parse_lorebook(
        native_book(
            {
                "0": {
                    "uid": 0,
                    "key": ["猴子"],
                    "content": "猴子提到了水帘洞。",
                },
                "1": {
                    "uid": 1,
                    "key": ["水帘洞"],
                    "content": "洞中有石桌。",
                },
            }
        )
    )
    assert active_uids(select_entries(book, "猴子", max_chars=1000)) == ["0"]
    assert set(
        active_uids(
            select_entries(book, "猴子", max_chars=1000, recursion_steps=1)
        )
    ) == {"0", "1"}
