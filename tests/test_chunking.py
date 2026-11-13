from mem_ol.pipelines.chunking import SemanticChunker
from mem_ol.pipelines.memory import build_dialogue_text


def fake_embed(texts):
    vectors = []
    for text in texts:
        if "战斗" in text:
            vectors.append([1.0, 0.0, 0.0])
        elif "集市" in text:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def make_chunker() -> SemanticChunker:
    return SemanticChunker(
        fake_embed,
        parent_size=1600,
        parent_min=600,
        parent_max=2200,
        child_size=400,
        child_min=150,
        child_max=600,
        child_overlap=80,
        semantic_threshold=0.45,
        # 这组用例只验证语义边界与编号，固定粗粒度单元以保持断言稳定；
        # 新的 600/80 默认粒度由 test_default_chunk_sizes_are_small 覆盖。
        max_unit_size=400,
    )


def test_parent_child_chunking_uses_semantic_boundaries():
    text = "\n".join(
        ["战斗" * 200, "战斗" * 200, "集市" * 200, "集市" * 200]
    )
    chunks = make_chunker().split(text, batch_seq=1)

    parents = [chunk for chunk in chunks if chunk.level == "parent"]
    children = [chunk for chunk in chunks if chunk.level == "child"]

    assert len(parents) == 2
    assert len(children) == 4
    assert parents[0].chunk_no == "B0001-P001"
    assert parents[1].chunk_no == "B0001-P002"
    assert children[0].chunk_no == "B0001-P001-C001"
    assert children[2].chunk_no == "B0001-P002-C001"

    for parent in parents:
        parent_children = [
            child
            for child in children
            if child.parent_index == parent.parent_index
        ]
        assert parent_children
        for child in parent_children:
            assert parent.char_start <= child.char_start
            assert child.char_end <= parent.char_end

    assert [chunk.seq_no for chunk in chunks] == list(
        range(1, len(chunks) + 1)
    )


def test_default_chunk_sizes_are_small():
    chunker = SemanticChunker(fake_embed)
    text = "。".join(
        ["这是一段用于测试的叙事文本，包含人物、地点与动作"] * 80
    )
    chunks = chunker.split(text, batch_seq=3)
    parents = [chunk for chunk in chunks if chunk.level == "parent"]
    children = [chunk for chunk in chunks if chunk.level == "child"]

    assert parents and children
    assert max(len(chunk.content) for chunk in parents) <= 900
    assert max(len(chunk.content) for chunk in children) <= 120
    assert (
        sum(len(chunk.content) for chunk in children) / len(children) <= 100
    )


def test_chunk_no_contains_turn():
    chunker = make_chunker()
    chunks = chunker.split("战斗" * 400, batch_seq=2, turn_no=3)
    assert chunks
    assert all(chunk.turn_no == 3 for chunk in chunks)
    assert all(chunk.chunk_no.startswith("B0002-T0003-") for chunk in chunks)


def test_build_dialogue_text_tracks_messages():
    messages = [
        {"id": "m1", "role": "user", "turn_no": 1, "content": "战斗" * 200},
        {"id": "m2", "role": "assistant", "turn_no": 1, "content": "战斗" * 200},
        {"id": "m3", "role": "user", "turn_no": 2, "content": "集市" * 200},
        {"id": "m4", "role": "assistant", "turn_no": 2, "content": "集市" * 200},
    ]
    text, segments = build_dialogue_text(messages)

    assert "【第1轮 · 用户】" in text
    assert "【第2轮 · GM】" in text
    assert len(segments) == 4

    chunks = make_chunker().split(text, segments, batch_seq=1)
    first_parent = next(chunk for chunk in chunks if chunk.level == "parent")
    assert first_parent.turn_start == 1
    assert first_parent.turn_end == 1
    assert first_parent.message_ids == ["m1", "m2"]
