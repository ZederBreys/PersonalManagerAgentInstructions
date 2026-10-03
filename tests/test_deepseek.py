"""Unit tests for the DeepSeek client (mocked HTTP)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.deepseek import Classification, DeepSeekClient, DeepSeekError

API_KEY = "sk-test-secret-key"


def _client(
    handler: Any,
    *,
    model: str = "deepseek-chat",
    **kwargs: Any,
) -> DeepSeekClient:
    transport = httpx.MockTransport(handler)
    return DeepSeekClient(API_KEY, transport=transport, model=model, **kwargs)


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


CLASSIFICATION_JSON = (
    '{"category": "financial", "importance": "high", '
    '"summary": "Счёт за подписку", "action_required": true}'
)


def test_classify_message_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        body = request.read().decode()
        assert "deepseek-chat" in body
        assert '"category"' not in body  # prompt only, no result keys yet
        return _chat_response(CLASSIFICATION_JSON)

    async def run() -> None:
        client = _client(handler)
        result = await client.classify_message(subject="S", body="B")
        assert isinstance(result, Classification)
        assert result.category == "financial"
        assert result.importance == "high"
        assert result.summary == "Счёт за подписку"
        assert result.action_required is True
        await client.aclose()

    asyncio.run(run())


def test_classify_message_http_5xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "server exploded"}})

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError) as excinfo:
            await client.classify_message(subject="S", body="B")
        assert excinfo.value.http_status == 500
        assert "server exploded" in str(excinfo.value)
        await client.aclose()

    asyncio.run(run())


def test_classify_message_http_4xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError) as excinfo:
            await client.classify_message(subject="S", body="B")
        assert excinfo.value.http_status == 401
        await client.aclose()

    asyncio.run(run())


def test_classify_message_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError):
            await client.classify_message(subject="S", body="B")
        await client.aclose()

    asyncio.run(run())


def test_classify_message_network_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError):
            await client.classify_message(subject="S", body="B")
        await client.aclose()

    asyncio.run(run())


def test_classify_message_invalid_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("this is not json")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError) as excinfo:
            await client.classify_message(subject="S", body="B")
        assert "invalid JSON" in str(excinfo.value)
        await client.aclose()

    asyncio.run(run())


def test_classify_message_invalid_structured_output() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response('{"category": "not-a-category"}')

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError) as excinfo:
            await client.classify_message(subject="S", body="B")
        assert "invalid classification" in str(excinfo.value)
        await client.aclose()

    asyncio.run(run())


def test_classify_message_unexpected_response_shape() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError):
            await client.classify_message(subject="S", body="B")
        await client.aclose()

    asyncio.run(run())


def test_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        DeepSeekClient("")


def test_api_key_not_in_exception_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(DeepSeekError) as excinfo:
            await client.classify_message(subject="S", body="B")
        assert API_KEY not in str(excinfo.value)
        assert API_KEY not in repr(excinfo.value)
        await client.aclose()

    asyncio.run(run())


def test_api_key_not_logged_on_error(caplog: pytest.LogCaptureFixture) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async def run() -> None:
        client = _client(handler)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(DeepSeekError):
                await client.classify_message(subject="S", body="B")
        assert API_KEY not in caplog.text
        await client.aclose()

    asyncio.run(run())


def test_classify_message_sends_response_format_json_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert '"response_format"' in body
        assert '"json_object"' in body
        return _chat_response(CLASSIFICATION_JSON)

    async def run() -> None:
        client = _client(handler)
        await client.classify_message(subject="S", body="B")
        await client.aclose()

    asyncio.run(run())


def test_classification_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        Classification.model_validate(
            {
                "category": "unknown",
                "importance": "high",
                "summary": "x",
                "action_required": True,
            }
        )


def test_classification_rejects_unknown_importance() -> None:
    with pytest.raises(ValidationError):
        Classification.model_validate(
            {
                "category": "financial",
                "importance": "urgent",
                "summary": "x",
                "action_required": True,
            }
        )


def test_classification_rejects_string_boolean() -> None:
    for bad in ("true", "false", "yes", "no", "1", "0"):
        with pytest.raises(ValidationError):
            Classification.model_validate(
                {
                    "category": "financial",
                    "importance": "high",
                    "summary": "x",
                    "action_required": bad,
                }
            )


def test_classification_rejects_integer_boolean() -> None:
    for bad in (1, 0):
        with pytest.raises(ValidationError):
            Classification.model_validate(
                {
                    "category": "financial",
                    "importance": "high",
                    "summary": "x",
                    "action_required": bad,
                }
            )


def test_classification_rejects_wrong_json_type() -> None:
    for payload in (None, [], "hello", 123, 1.5):
        with pytest.raises(ValidationError):
            Classification.model_validate(payload)


def test_classification_rejects_missing_fields() -> None:
    with pytest.raises(ValidationError):
        Classification.model_validate({"category": "financial"})


def test_classification_rejects_null_fields() -> None:
    with pytest.raises(ValidationError):
        Classification.model_validate(
            {
                "category": "financial",
                "importance": "high",
                "summary": None,
                "action_required": None,
            }
        )


def test_classification_ignores_extra_fields() -> None:
    result = Classification.model_validate(
        {
            "category": "financial",
            "importance": "high",
            "summary": "x",
            "action_required": True,
            "unexpected": "field",
        }
    )
    assert result.category == "financial"
    assert result.importance == "high"
