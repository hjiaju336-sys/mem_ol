import json

from mem_ol.log import get_logger, log_context, reset_logging, setup_logging


def test_json_log_format(tmp_path):
    log_file = tmp_path / "test.jsonl"
    setup_logging(level="DEBUG", log_file=str(log_file), console=False, force=True)
    try:
        logger = get_logger("mem_ol.test")
        with log_context(thread_id="t-1", user_id="u-1"):
            logger.info(
                "批次完成",
                extra={
                    "event": "memory.batch.done",
                    "batch_id": "b-1",
                    "cost_ms": 12,
                },
            )

        line = log_file.read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(line)

        assert payload["level"] == "INFO"
        assert payload["logger"] == "mem_ol.test"
        assert payload["message"] == "批次完成"
        assert payload["thread_id"] == "t-1"
        assert payload["user_id"] == "u-1"
        assert payload["batch_id"] == "b-1"
        assert payload["trace_id"] is None
        assert payload["event"] == "memory.batch.done"
        assert payload["extra"]["cost_ms"] == 12
    finally:
        reset_logging()
