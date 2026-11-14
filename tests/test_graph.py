from langgraph.checkpoint.memory import MemorySaver

from mem_ol.graph.conversation import ConversationGraph
from mem_ol.domain import AP2AnalysisResult


class FakeStore:
    def __init__(self):
        self.turn = 0
        self.checkpoints = []

    def next_turn_no(self, thread_id):
        self.turn += 1
        return self.turn

    def count_un_ingested_turns(self, thread_id):
        return 0

    def update_turn_checkpoint(self, thread_id, turn_no, checkpoint_id):
        self.checkpoints.append((turn_no, checkpoint_id))

    def get_recent_turns(self, thread_id, turns):
        return []


class FakeMetaPreTurn:
    def classify(
        self,
        thread_id,
        user_message,
        *,
        turn_no=None,
        pending_instructions=(),
    ):
        return AP2AnalysisResult(
            channel="meta",
            intent="default",
            meta_kind="instruction",
        )

    def meta_reply(self, question, recent=()):
        return "meta-answer"


class FakePreTurn:
    def run(self, thread_id, user_message, *, turn_no=None):
        return {
            "trace_id": f"trace-{turn_no}",
            "hypothetical_entities": [],
            "entity_candidates": [],
            "context_candidates": [],
            "graph_paths": [],
        }


class FakeJudge:
    def propose(self, **kwargs):
        return {"effects": [], "appearances": []}


class FakeDialogue:
    def generate(
        self,
        thread_id,
        user_message,
        *,
        pre_turn=None,
        turn_no=None,
        client_context=None,
        state_view=None,
        resolution=None,
    ):
        return {
            "reply": f"reply-{turn_no}",
            "turn_no": turn_no,
            "history_messages": 0,
            "retrieved": [],
            "trace_id": (pre_turn or {}).get("trace_id"),
            "entity_candidates": [],
            "graph_paths": [],
        }

    def persist_turn(
        self, thread_id, user_message, reply, turn_no, *, checkpoint_id=None
    ):
        return [f"u-{turn_no}", f"a-{turn_no}"]


def test_graph_turns_increment_and_checkpoint_is_written():
    store = FakeStore()
    graph = ConversationGraph(
        store=store,
        tools=None,
        checkpointer=MemorySaver(),
        pre_turn_service=FakePreTurn(),
        dialogue_engine=FakeDialogue(),
        state_judge=FakeJudge(),
    )

    first = graph.run("thread-1", "hello")
    second = graph.run("thread-1", "again")

    assert first["turn_no"] == 1
    assert first["reply"] == "reply-1"
    assert first["turn_checkpoint_id"]
    assert first["message_ids"] == ["u-1", "a-1"]
    assert second["turn_no"] == 2
    assert second["reply"] == "reply-2"
    assert second["turn_checkpoint_id"]
    assert second["turn_checkpoint_id"] != first["turn_checkpoint_id"]
    assert [turn for turn, _ in store.checkpoints] == [1, 2]


def test_meta_instruction_does_not_consume_turn_or_checkpoint():
    store = FakeStore()
    graph = ConversationGraph(
        store=store,
        tools=None,
        checkpointer=MemorySaver(),
        pre_turn_service=FakeMetaPreTurn(),
        dialogue_engine=FakeDialogue(),
        state_judge=FakeJudge(),
    )

    result = graph.run("thread-1", "写短点")

    assert result["channel"] == "meta"
    assert result["meta_kind"] == "instruction"
    assert result["turn_no"] is None
    assert result["message_ids"] == []
    assert store.turn == 0
    assert store.checkpoints == []


class FakeEntityTools:
    def entity_resolve(self, thread_id, name):
        if name == "守卫":
            return {"found": True, "entity": {"entity_id": "e-guard"}}
        return {"found": False, "entity": None}

    def characters_created_at(self, thread_id, turn_no):
        if turn_no == 2:
            return [
                {
                    "entity_id": "e-yaolao",
                    "canonical_name": "药老",
                    "entity_type": "character",
                }
            ]
        return []


def test_sync_appearing_characters_adds_actor_and_binds_entity():
    graph = ConversationGraph.__new__(ConversationGraph)
    graph.tools = FakeEntityTools()
    state = {"actors": {"player": {"name": "玩家"}}}
    graph._sync_appearing_characters("thread-1", state, 3)
    assert state["actors"]["npc:药老"]["entity_id"] == "e-yaolao"


def test_bind_entities_attaches_entity_id_by_name():
    graph = ConversationGraph.__new__(ConversationGraph)
    graph.tools = FakeEntityTools()
    state = {"actors": {"player": {"name": "玩家"}, "npc:守卫": {"name": "守卫"}}}
    graph._bind_entities("thread-1", state, ["守卫", "路人", None])
    assert state["actors"]["npc:守卫"]["entity_id"] == "e-guard"
    assert "entity_id" not in state["actors"]["player"]


def test_reconcile_creates_actors_for_appearances():
    graph = ConversationGraph.__new__(ConversationGraph)
    graph.tools = FakeEntityTools()
    out = graph._reconcile(
        {
            "thread_id": "thread-1",
            "appearances": ["药老"],
            "effects": [],
            "state_after": {"actors": {"player": {"name": "玩家"}}},
        }
    )
    assert out["state_after"]["actors"]["npc:药老"]["name"] == "药老"
