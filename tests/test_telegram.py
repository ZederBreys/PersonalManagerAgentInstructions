"""Unit tests for the Telegram Bot API transport client (mocked HTTP)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from app.telegram import TelegramAPIError, TelegramClient

TOKEN = "123456:ABC-DEF-SECRET"


def _client(
    handler: Any,
    *,
    poll_timeout: int = 30,
    **kwargs: Any,
) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    return TelegramClient(TOKEN, transport=transport, poll_timeout=poll_timeout, **kwargs)


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# --- send_message ---


def test_send_message_uses_correct_endpoint_and_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        body = request.read().decode()
        assert '"chat_id":42' in body
        assert '"text":"hello"' in body
        return _ok({"message_id": 7})

    async def run() -> None:
        client = _client(handler)
        result = await client.send_message(chat_id=42, text="hello")
        assert result == {"message_id": 7}
        await client.aclose()

    asyncio.run(run())


def test_send_message_returns_result_on_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"message_id": 1, "text": "hi"})

    async def run() -> None:
        client = _client(handler)
        result = await client.send_message(chat_id=1, text="hi")
        assert result["message_id"] == 1
        await client.aclose()

    asyncio.run(run())


def test_send_message_ok_false_raises_telegram_api_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
        )

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(TelegramAPIError) as excinfo:
            await client.send_message(chat_id=1, text="hi")
        assert excinfo.value.error_code == 400
        assert excinfo.value.http_status == 400
        assert "Bad Request" in excinfo.value.description
        await client.aclose()

    asyncio.run(run())


def test_send_message_network_error_is_not_swallowed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(TelegramAPIError):
            await client.send_message(chat_id=1, text="hi")
        await client.aclose()

    asyncio.run(run())


def test_send_message_timeout_error_is_not_swallowed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(TelegramAPIError):
            await client.send_message(chat_id=1, text="hi")
        await client.aclose()

    asyncio.run(run())


# --- get_updates ---


def test_get_updates_passes_offset_and_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/bot{TOKEN}/getUpdates"
        params = dict(request.url.params)
        assert params["offset"] == "5"
        assert params["timeout"] == "10"
        return _ok([{"update_id": 1}])

    async def run() -> None:
        client = _client(handler)
        result = await client.get_updates(offset=5, timeout=10)
        assert result == [{"update_id": 1}]
        await client.aclose()

    asyncio.run(run())


def test_get_updates_uses_default_poll_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["timeout"] == "30"
        assert "offset" not in params
        return _ok([])

    async def run() -> None:
        client = _client(handler, poll_timeout=30)
        result = await client.get_updates()
        assert result == []
        await client.aclose()

    asyncio.run(run())


def test_get_updates_returns_update_list() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"update_id": 100}, {"update_id": 101}])

    async def run() -> None:
        client = _client(handler)
        result = await client.get_updates()
        assert len(result) == 2
        await client.aclose()

    asyncio.run(run())


def test_get_updates_api_error_raises() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
        )

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(TelegramAPIError) as excinfo:
            await client.get_updates()
        assert excinfo.value.error_code == 401
        await client.aclose()

    asyncio.run(run())


# --- security ---


def test_token_not_in_exception_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async def run() -> None:
        client = _client(handler)
        with pytest.raises(TelegramAPIError) as excinfo:
            await client.send_message(chat_id=1, text="hi")
        assert TOKEN not in str(excinfo.value)
        assert TOKEN not in repr(excinfo.value)
        await client.aclose()

    asyncio.run(run())


def test_token_not_logged_on_error(caplog: pytest.LogCaptureFixture) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "nope"}
        )

    async def run() -> None:
        client = _client(handler)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(TelegramAPIError):
                await client.send_message(chat_id=1, text="hi")
        assert TOKEN not in caplog.text
        await client.aclose()

    asyncio.run(run())


# --- lifecycle ---


def test_single_client_reused_across_requests() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _ok({"ok": True})

    async def run() -> None:
        client = _client(handler)
        await client.send_message(chat_id=1, text="a")
        await client.send_message(chat_id=1, text="b")
        await client.get_updates()
        assert len(calls) == 3
        assert client._client.is_closed is False
        await client.aclose()

    asyncio.run(run())


def test_async_context_manager_closes_client() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _ok([])

    async def run() -> None:
        client = _client(handler)
        async with client as telegram:
            assert telegram is client
            await telegram.get_updates()
        assert client._client.is_closed is True

    asyncio.run(run())


def test_client_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        TelegramClient("")
