from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import mem_ol.services.llm as llm_module
from mem_ol.services.llm import LLMService


class _RateLimited(Exception):
    status_code = 429


def _service_with_create(create):
    service = object.__new__(LLMService)
    service.settings = SimpleNamespace(
        openai_model="fake", openai_temperature=0.1
    )
    service.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    return service


def test_create_retries_rate_limit(monkeypatch):
    calls = {"count": 0}

    def create(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _RateLimited("busy")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok")
                )
            ]
        )

    monkeypatch.setattr(
        llm_module, "time", SimpleNamespace(sleep=lambda _: None)
    )
    service = _service_with_create(create)

    response = service._create(
        messages=[{"role": "user", "content": "x"}],
        temperature=0,
        max_tokens=5,
        timeout=1,
    )

    assert calls["count"] == 2
    assert response.choices[0].message.content == "ok"


def test_create_does_not_retry_bad_request():
    calls = {"count": 0}

    class _BadRequest(Exception):
        status_code = 400

    def create(**kwargs):
        calls["count"] += 1
        raise _BadRequest("bad")

    service = _service_with_create(create)

    with pytest.raises(_BadRequest):
        service._create(
            messages=[{"role": "user", "content": "x"}],
            temperature=0,
            max_tokens=5,
            timeout=1,
        )
    assert calls["count"] == 1


class _SimpleResult(BaseModel):
    ok: bool


def test_chat_json_model_repairs_invalid_schema():
    service = object.__new__(LLMService)
    calls: list[list[dict]] = []

    def fake_chat_json(messages, **kwargs):
        calls.append(list(messages))
        return {"bad": 1} if len(calls) == 1 else {"ok": True}

    service.chat_json = fake_chat_json

    result = service.chat_json_model(
        _SimpleResult, [{"role": "user", "content": "x"}]
    )

    assert result.ok is True
    assert len(calls) == 2
    assert "结构校验" in calls[1][-1]["content"]
