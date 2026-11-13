from types import SimpleNamespace

from mem_ol.services.world_import import (
    WorldAttribute,
    WorldEntity,
    WorldImportExtraction,
    WorldRelation,
    WorldImportService,
    _attach_source_orders,
    _build_decisions,
    _merge_extractions,
    _split_text_windows,
)


def test_build_decisions_from_consolidated_extraction():
    extraction = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="水帘洞",
                aliases=["洞府"],
                type="location",
                description="花果山瀑布后的石洞，洞内有石桌石碗。",
                attributes=[
                    WorldAttribute(key="located_in", value="花果山")
                ],
            ),
            WorldEntity(
                canonical_name="花果山",
                type="location",
                description="东胜神洲名山。",
            ),
        ],
        relations=[
            WorldRelation(
                source_ref="洞府",
                target_ref="花果山",
                relation_type="located_in",
            )
        ],
    )

    decisions = _build_decisions(extraction)
    by_name = {decision.canonical_name: decision for decision in decisions}
    assert by_name["水帘洞"].aliases == ["洞府"]
    assert by_name["水帘洞"].relations[0].target_ref == "花果山"


def test_split_text_windows_by_paragraph():
    text = "\n\n".join(["x" * 100 for _ in range(5)])
    windows = _split_text_windows(text, max_chars=250)
    assert [window.count("x" * 100) for window in windows] == [2, 2, 1]


def test_split_text_windows_handles_single_newlines():
    text = "\n".join(["第%d段。" % index + "内容" * 500 for index in range(3)])
    windows = _split_text_windows(text, max_chars=800)
    assert len(windows) > 1
    assert all(len(window) <= 800 for window in windows)


def test_split_text_windows_hard_splits_without_punctuation():
    windows = _split_text_windows("字" * 2500, max_chars=800)
    assert [len(window) for window in windows] == [800, 800, 800, 100]


def test_extract_windows_parallel_preserves_order_and_reports_failure():
    service = object.__new__(WorldImportService)
    service.settings = SimpleNamespace(world_extract_workers=2)

    def fake_extract(window_text: str, source_name: str | None):
        if window_text == "bad":
            raise ValueError("boom")
        return WorldImportExtraction(
            entities=[
                WorldEntity(canonical_name=window_text, type="other")
            ]
        )

    service._extract_window_with_retry = fake_extract
    results, failed = service._extract_windows(["a", "bad", "c"], None)

    assert [result.entities[0].canonical_name for result in results] == [
        "a",
        "c",
    ]
    assert failed == [{"index": 1, "chars": 3, "error": "boom"}]


def test_attach_source_orders_from_raw_extractions():
    raw = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                aliases=["炎帝"],
                type="character",
                attributes=[
                    WorldAttribute(
                        key="state", value="废物", source_order=3000
                    )
                ],
            )
        ]
    )
    consolidated = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                aliases=["炎帝"],
                type="character",
                attributes=[WorldAttribute(key="state", value="废物")],
            )
        ]
    )

    _attach_source_orders(consolidated, [raw])

    assert consolidated.entities[0].attributes[0].source_order == 3000


def test_build_decisions_offsets_source_order_by_batch():
    extraction = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                type="character",
                attributes=[
                    WorldAttribute(
                        key="state", value="废物", source_order=1000
                    )
                ],
            ),
            WorldEntity(canonical_name="萧家", type="faction"),
        ],
        relations=[
            WorldRelation(
                source_ref="萧炎",
                target_ref="萧家",
                relation_type="member_of",
                source_order=1500,
            )
        ],
    )

    decisions = _build_decisions(extraction, batch_seq=2)
    by_name = {decision.canonical_name: decision for decision in decisions}

    assert (
        by_name["萧炎"].attribute_changes[0].source_order
        == 2_000_000 + 1000
    )
    assert (
        by_name["萧炎"].relations[0].source_order
        == 2_000_000 + 1500
    )


def test_build_decisions_matches_existing_entities():
    extraction = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                type="character",
                attributes=[
                    WorldAttribute(
                        key="state", value="斗者", source_order=1000
                    )
                ],
            ),
            WorldEntity(canonical_name="萧家", type="faction"),
        ],
        relations=[
            WorldRelation(
                source_ref="萧炎",
                target_ref="萧家",
                relation_type="member_of",
                source_order=1500,
            )
        ],
    )
    existing = {
        "萧炎": "11111111-1111-1111-1111-111111111111",
        "萧家": "22222222-2222-2222-2222-222222222222",
    }

    decisions = _build_decisions(extraction, batch_seq=2, existing_by_name=existing)
    by_name = {decision.canonical_name: decision for decision in decisions}

    assert by_name["萧炎"].action == "update"
    assert (
        by_name["萧炎"].matched_entity_id
        == "11111111-1111-1111-1111-111111111111"
    )
    assert (
        by_name["萧炎"].relations[0].target_ref
        == "22222222-2222-2222-2222-222222222222"
    )


def test_merge_extractions_keeps_latest_source_order():
    first = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                type="character",
                attributes=[
                    WorldAttribute(
                        key="state", value="废物", source_order=1000
                    )
                ],
            )
        ]
    )
    second = WorldImportExtraction(
        entities=[
            WorldEntity(
                canonical_name="萧炎",
                type="character",
                attributes=[
                    WorldAttribute(
                        key="state", value="废物", source_order=4000
                    )
                ],
            )
        ]
    )

    merged, _ = _merge_extractions([first, second])

    assert merged["萧炎"].attributes[0].source_order == 4000
