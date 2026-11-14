import uuid
from datetime import datetime, timezone

import pytest

from mem_ol.services.rollback import RollbackService


class FakeStore:
    def __init__(self):
        self.watermark = datetime.now(timezone.utc)
        self.calls = []

    def get_thread(self, thread_id):
        return {"id": thread_id}

    def get_target_turn_watermark(self, thread_id, target_turn):
        return self.watermark

    def get_target_checkpoint(self, thread_id, target_turn):
        return "checkpoint-1"

    def mark_messages_rolled_back(self, thread_id, target_turn):
        self.calls.append("messages")
        return 4

    def delete_chunks_after_turn(self, thread_id, target_turn):
        self.calls.append("chunks")
        return 10

    def delete_entity_index_after_turn(self, thread_id, target_turn):
        self.calls.append("entity_index")
        return 3

    def delete_mentions_after_turn(self, thread_id, target_turn):
        self.calls.append("mentions")
        return 2

    def delete_traces_after_turn(self, thread_id, target_turn):
        self.calls.append("traces")
        return 5

    def delete_plot_nodes_after_turn(self, thread_id, target_turn):
        self.calls.append("plot_nodes")
        return 2

    def delete_state_after_turn(self, thread_id, target_turn):
        self.calls.append("state_events")
        return 1

    def refresh_batch_chunk_counts(self, thread_id):
        self.calls.append("batches")

    def upsert_thread_state(self, thread_id, watermark):
        self.calls.append("state")


class FakeNeo4j:
    def rollback_after_turn(self, thread_id, target_turn):
        return {"attributes": 6, "relations": 3, "entities": 2}


def test_rollback_orchestrates_all_stores():
    store = FakeStore()
    service = RollbackService(store, FakeNeo4j())
    result = service.rollback(str(uuid.uuid4()), 2)

    assert result["target_turn"] == 2
    assert result["checkpoint_id"] == "checkpoint-1"
    assert result["rolled_back_messages"] == 4
    assert result["deleted_chunks"] == 10
    assert result["deleted_graph_entities"] == 2
    assert store.calls == [
        "messages",
        "chunks",
        "entity_index",
        "mentions",
        "traces",
        "plot_nodes",
        "state_events",
        "batches",
        "state",
    ]


def test_rollback_rejects_missing_target_turn():
    store = FakeStore()
    store.get_target_turn_watermark = lambda thread_id, turn: None
    service = RollbackService(store, FakeNeo4j())
    with pytest.raises(ValueError):
        service.rollback(str(uuid.uuid4()), 3)
