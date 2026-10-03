"""Minimal async HTTP client for the DeepSeek API (chat completions).

Uses the official DeepSeek API directly over ``httpx`` (no OpenRouter, no AI
framework). The API key is only ever read from configuration and is never
logged or included in exception text; transport errors are reported by
exception *type* only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from app.deepseek.prompt import build_classification_messages
from app.deepseek.schemas import Classification

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekError(Exception):
    """Raised for any DeepSeek failure (HTTP, transport, timeout, bad output).

    ``message`` is a human-readable description (never the API key); ``http_status``
    is the HTTP status code when a response was received.
    """

    def __init__(
        self,
        *,
        message: str = "DeepSeek API error",
        http_status: int | None = None,
    ) -> None:
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class DeepSeekClient:
    """Async DeepSeek chat-completions client using ``httpx.AsyncClient``."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        http_timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")

        # httpx logs full request URLs (including the Authorization header's
        # bearer token) at INFO level. Suppress that so the key never leaks.
        for _name in ("httpx", "httpcore"):
            logging.getLogger(_name).setLevel(logging.WARNING)

        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(http_timeout),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def __aenter__(self) -> DeepSeekClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def classify_message(
        self,
        *,
        subject: str | None = None,
        body: str | None = None,
    ) -> Classification:
        """Classify a message and return a validated :class:`Classification`.

        Raises :class:`DeepSeekError` if the request fails or the response is
        not a valid classification.
        """

        messages = build_classification_messages(subject=subject, body=body)
        content = await self.chat(messages)
        return _parse_classification(content)

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """Send a chat-completions request and return the assistant's text."""

        data = await self._request(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError(message="Unexpected DeepSeek response shape") from exc
        if not isinstance(content, str):
            raise DeepSeekError(message="Unexpected DeepSeek response shape")
        return content

    async def _request(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise DeepSeekError(
                message=f"DeepSeek request failed: {type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            raise DeepSeekError(
                message=self._error_message(response),
                http_status=response.status_code,
            )

        try:
            data = response.json()
        except ValueError:
            raise DeepSeekError(
                message="Invalid JSON response from DeepSeek API",
                http_status=response.status_code,
            ) from None

        if not isinstance(data, dict):
            raise DeepSeekError(
                message="Unexpected DeepSeek response shape",
                http_status=response.status_code,
            )
        return data

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict) and err.get("message"):
                    return str(err["message"])
        except ValueError:
            pass
        return f"DeepSeek API returned HTTP {response.status_code}"


def _parse_classification(content: str) -> Classification:
    """Parse the assistant's JSON text into a validated ``Classification``."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DeepSeekError(message="DeepSeek returned invalid JSON") from exc

    try:
        return Classification.model_validate(payload)
    except ValidationError as exc:
        raise DeepSeekError(message="DeepSeek returned an invalid classification") from exc
