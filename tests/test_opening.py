from mem_ol.services.opening import OpeningService


class FakeStore:
    def __init__(self, opening=None, messages=None):
        self.opening = opening
        self.messages = messages or []
        self.added = []

    def get_opening(self, thread_id):
        return self.opening

    def list_messages(self, thread_id):
        return self.messages

    def add_message(self, thread_id, role, content, turn_no, **kwargs):
        row = {
            "id": "m-1",
            "role": role,
            "content": content,
            "turn_no": turn_no,
        }
        self.added.append(row)
        return row


class FakeRetrieval:
    def retrieve(self, thread_id, query, k=4, **kwargs):
        return [
            {
                "chunk_no": "B0001-P001-C001",
                "content": "花果山瀑布后的石洞。",
                "parent_content": "花果山瀑布后的石洞，洞内有石桌石碗。",
            }
        ]


class FakeLLM:
    def chat(self, messages, **kwargs):
        return "你站在花果山脚下，水声从远处传来。"


def test_opening_generated_and_persisted_at_turn_zero():
    store = FakeStore()
    service = OpeningService(store, retrieval=FakeRetrieval(), llm=FakeLLM())
    result = service.generate("thread-1")
    assert result["reply"] == "你站在花果山脚下，水声从远处传来。"
    assert store.added[0]["turn_no"] == 0
    assert store.added[0]["role"] == "assistant"


def test_existing_opening_is_reused():
    store = FakeStore(
        opening={
            "id": "m-0",
            "role": "assistant",
            "content": "旧开场白",
            "turn_no": 0,
        }
    )
    service = OpeningService(store, retrieval=FakeRetrieval(), llm=FakeLLM())
    result = service.generate("thread-1")
    assert result["reply"] == "旧开场白"
    assert result["exists"] is True
    assert store.added == []


def test_do_not_insert_opening_mid_conversation():
    store = FakeStore(messages=[{"id": "m-1", "turn_no": 1}])
    service = OpeningService(store, retrieval=FakeRetrieval(), llm=FakeLLM())
    assert service.generate("thread-1") is None
    assert store.added == []
