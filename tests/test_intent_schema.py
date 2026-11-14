from mem_ol.domain import AP2ContextResult, TurnDirective


def test_invalid_channel_and_intent_fall_back():
    result = AP2ContextResult(channel="unknown", intent="fight")
    assert result.channel == "narrative"
    assert result.intent == "default"


def test_directive_values_are_normalized():
    directive = TurnDirective(
        length="SHORT",
        pace="fastest",
        focus="action",
        options="none",
        tone="",
        notes="多写心理",
    )
    assert directive.length == "short"
    assert directive.pace is None
    assert directive.focus == "action"
    assert directive.options == "none"
    assert directive.tone is None
    assert directive.notes == "多写心理"


def test_none_fields_are_tolerated():
    result = AP2ContextResult(
        channel=None, intent=None, directives=None, meta_kind=None
    )
    assert result.channel == "narrative"
    assert result.intent == "default"
    assert result.meta_kind == "instruction"
    assert result.directives.is_empty()


def test_directive_empty():
    assert TurnDirective().is_empty()
    assert not TurnDirective(length="short").is_empty()


def test_overlong_ap2_text_is_clipped_not_rejected():
    # 超长不能整份失败，否则分流会降级成叙事，纯 meta 被落库。
    result = AP2ContextResult(
        channel="meta",
        context_summary="概" * 500,
        rewritten_query="改" * 1200,
        narrative_query="叙" * 2000,
        meta_question="问" * 1200,
    )
    assert result.channel == "meta"
    assert len(result.context_summary) == 300
    assert len(result.rewritten_query) == 800
    assert len(result.narrative_query) == 1200
    assert len(result.meta_question) == 800


def test_overlong_entities_are_clipped():
    from mem_ol.domain import AP2EntitiesResult

    result = AP2EntitiesResult(entities=[f"词{i}" for i in range(20)])
    assert len(result.entities) == 12
