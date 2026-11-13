from mem_ol.services.query_enhancer import QueryEnhancer


def test_query_enhancer_placeholder_keeps_original_query():
    analysis = QueryEnhancer().analyze("我回到水帘洞")
    assert analysis.original_query == "我回到水帘洞"
    assert analysis.rewritten_query == "我回到水帘洞"
    assert analysis.intent == "unknown"
    assert analysis.route_hint == "default"
