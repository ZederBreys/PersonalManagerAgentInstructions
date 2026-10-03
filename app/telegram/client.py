"""Minimal async HTTP client for the Telegram Bot API.

Only the two methods this project needs are exposed: ``getUpdates`` (long
polling) and ``sendMessage``. A single :class:`httpx.AsyncClient` is created per
:class:`TelegramClient` and reused across requests.

Security: the bot token is never logged and never appears in exception text;
transport errors are reported by exception *type* only, never by URL.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT = 30
# HTTP timeout must outlive the Telegram long-poll timeout so the HTTP client
# never cuts the request off before Telegram itself returns.
_POLL_HTTP_BUFFER_SECONDS = 10.0


class TelegramAPIError(Exception):
    """Raised when the Bot API returns ``ok: false`` or a transport error.

    ``description`` is a human-readable message (never the token/URL);
    ``error_code`` is Telegram's numeric error code when present; ``http_status``
    is the HTTP status code when a response was received.
    """

    def __init__(
        self,
        *,
        description: str = "Telegram API error",
        error_code: int | None = None,
        http_status: int | None = None,
    ) -> None:
        self.description = description
        self.error_code = error_code
        self.http_status = http_status
        super().__init__(description)


class TelegramClient:
    """Async Bot API client using ``httpx.AsyncClient``.

    Supports the async context manager protocol so the underlying HTTP client
    is closed deterministically::

        async with TelegramClient(token) as telegram:
            await telegram.send_message(chat_id, "hi")
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
        http_timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("telegram_bot_token is not configured")

        # httpx logs full request URLs (including the bot token) at INFO level.
        # Suppress that to guarantee the token never reaches application logs.
        for _name in ("httpx", "httpcore"):
            logging.getLogger(_name).setLevel(logging.WARNING)

        self._token = token
        self._poll_timeout = poll_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(http_timeout),
            transport=transport,
        )

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send_message(self, chat_id: int, text: str) -> Any:
        """Send ``text`` to ``chat_id``; return the parsed ``result`` payload."""
        data = await self._request(
            "sendMessage", json={"chat_id": chat_id, "text": text}
        )
        return data.get("result")

    async def get_updates(
        self, offset: int | None = None, timeout: int | None = None
    ) -> Any:
        """Long-poll for updates; return the parsed ``result`` list.

        ``timeout`` is Telegram's long-poll timeout (defaults to the client's
        ``poll_timeout``); the HTTP timeout is set slightly larger so the HTTP
        client never cuts off a pending poll.
        """
        poll = self._poll_timeout if timeout is None else timeout
        params: dict[str, int] = {"timeout": poll}
        if offset is not None:
            params["offset"] = offset
        request_timeout = httpx.Timeout(poll + _POLL_HTTP_BUFFER_SECONDS)
        data = await self._request("getUpdates", params=params, timeout=request_timeout)
        return data.get("result")

    async def _request(
        self,
        method: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        url = f"/bot{self._token}/{method}"
        try:
            if json is not None:
                response = await self._client.post(url, json=json, timeout=timeout)
            else:
                response = await self._client.get(url, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(
                description=f"Telegram request failed: {type(exc).__name__}",
            ) from exc

        try:
            data = response.json()
        except ValueError:
            raise TelegramAPIError(
                description="Invalid JSON response from Telegram API",
                http_status=response.status_code,
            ) from None

        if not isinstance(data, dict) or not data.get("ok"):
            error_code = data.get("error_code") if isinstance(data, dict) else None
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramAPIError(
                description=description or "Telegram API returned an unsuccessful response",
                error_code=error_code,
                http_status=response.status_code,
            )
        return data
