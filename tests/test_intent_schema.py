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
