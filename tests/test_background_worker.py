from mem_ol.services.task_worker import BackgroundWorker


class FakeStore:
    def __init__(self, task):
        self.task = task
        self.finished = []
        self.claimed = 0

    def claim_background_task(self):
        if self.task is None:
            return None
        task, self.task = self.task, None
        self.claimed += 1
        return task

    def finish_background_task(self, task_id, *, error=None):
        self.finished.append((task_id, error))
        return "done" if error is None else "pending"


def _worker(task):
    worker = BackgroundWorker.__new__(BackgroundWorker)
    worker.store = FakeStore(task)
    worker.tools = None
    worker.llm = None
    worker.settings = None
    return worker


def test_worker_dispatches_plot_task(monkeypatch):
    import mem_ol.services.task_worker as module

    calls = []
    monkeypatch.setattr(
        module.maintenance,
        "run_plot",
        lambda *args, **kwargs: calls.append(args[3]),
    )
    worker = _worker(
        {
            "id": "t1",
            "thread_id": "th",
            "kind": "plot",
            "payload": {"turn_no": 3},
            "attempts": 1,
        }
    )
    assert worker.run_once() is True
    assert calls == ["th"]
    assert worker.store.finished == [("t1", None)]


def test_worker_records_failure(monkeypatch):
    import mem_ol.services.task_worker as module

    def boom(*args, **kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(module.maintenance, "run_commit", boom)
    worker = _worker(
        {
            "id": "t2",
            "thread_id": "th",
            "kind": "commit",
            "payload": {},
            "attempts": 2,
        }
    )
    assert worker.run_once() is True
    assert worker.store.finished[0][0] == "t2"
    assert "commit failed" in worker.store.finished[0][1]


def test_worker_returns_false_when_queue_empty():
    worker = _worker(None)
    assert worker.run_once() is False
