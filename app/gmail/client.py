"""Minimal async client for the Gmail API (read-only).

The official ``google-api-python-client`` is synchronous, so every call is
offloaded to a worker thread via :func:`asyncio.to_thread` to keep the asyncio
event loop responsive. This thin layer exposes only the two operations the
importer needs — listing message ids (with pagination) and fetching a full
message — so the importer never touches ``.users().messages()...`` directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.gmail.auth import load_credentials


class GmailError(Exception):
    """Raised when a Gmail API call fails.

    ``message`` is the Google-provided error reason; ``http_status`` is the HTTP
    status code when a response was received. The original exception is always
    preserved in ``__cause__``.
    """

    def __init__(self, *, message: str = "Gmail API error", http_status: int | None = None) -> None:
        self.message = message
        self.http_status = http_status
        super().__init__(message)


@dataclass
class MessageList:
    """One page of listed Gmail message ids."""

    messages: list[str]
    next_page_token: str | None


def _http_status(exc: Any) -> int | None:
    return getattr(exc.resp, "status", None)


def _http_message(exc: Any) -> str:
    try:
        reason = exc._get_reason()  # noqa: SLF001 - only way to get the reason
    except Exception:  # pragma: no cover - defensive fallback
        reason = None
    return reason or str(exc)


def _transport_message(exc: Any) -> str:
    """A safe message for transport/network errors (never contains secrets)."""

    return f"Gmail transport error: {type(exc).__name__}"


class GmailClient:
    """Async wrapper around the synchronous Gmail API client.

    The underlying service is built lazily (on first use) and reused. For tests
    a fake ``service`` may be injected, which skips credential loading entirely.
    """

    def __init__(self, credentials: Any, *, service: Any | None = None) -> None:
        self._credentials = credentials
        self._service = service

    def _get_service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build("gmail", "v1", credentials=self._credentials)
        return self._service

    async def list_messages(
        self,
        *,
        q: str | None = None,
        max_results: int | None = None,
        page_token: str | None = None,
    ) -> MessageList:
        """List message ids (newest first), returning one page and its token."""

        return await asyncio.to_thread(self._list_messages, q, max_results, page_token)

    def _list_messages(
        self,
        q: str | None,
        max_results: int | None,
        page_token: str | None,
    ) -> MessageList:
        from google.auth.exceptions import TransportError
        from googleapiclient.errors import HttpError

        import httplib2

        try:
            kwargs: dict[str, Any] = {"userId": "me"}
            if q is not None:
                kwargs["q"] = q
            if max_results is not None:
                kwargs["maxResults"] = max_results
            if page_token is not None:
                kwargs["pageToken"] = page_token
            result = self._get_service().users().messages().list(**kwargs).execute()
        except HttpError as exc:
            raise GmailError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc
        # httplib2 re-raises socket-level failures (timeouts, connection resets,
        # SSL errors) as raw ``OSError`` subclasses, not ``HttpLib2Error``.
        except (httplib2.HttpLib2Error, TransportError, OSError) as exc:
            raise GmailError(
                message=_transport_message(exc), http_status=None
            ) from exc
        if not isinstance(result, dict):
            raise GmailError(message="Malformed Gmail list response", http_status=None)
        raw_messages = result.get("messages") or []
        if not isinstance(raw_messages, list):
            raise GmailError(message="Malformed Gmail list response", http_status=None)
        messages: list[str] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                raise GmailError(message="Malformed Gmail list response", http_status=None)
            message_id = item.get("id")
            if not isinstance(message_id, str) or not message_id:
                raise GmailError(message="Malformed Gmail list response", http_status=None)
            messages.append(message_id)
        next_page_token = result.get("nextPageToken")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise GmailError(message="Malformed Gmail list response", http_status=None)
        # An empty token means "no more pages"; treating it as a token would loop.
        return MessageList(messages=messages, next_page_token=next_page_token or None)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Fetch a full message (headers + body + ``internalDate``) by id."""

        return await asyncio.to_thread(self._get_message, message_id)

    def _get_message(self, message_id: str) -> dict[str, Any]:
        from google.auth.exceptions import TransportError
        from googleapiclient.errors import HttpError

        import httplib2

        try:
            result = (
                self._get_service()
                .users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            raise GmailError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc
        except (httplib2.HttpLib2Error, TransportError, OSError) as exc:
            raise GmailError(
                message=_transport_message(exc), http_status=None
            ) from exc
        if not isinstance(result, dict):
            raise GmailError(message="Malformed Gmail message response", http_status=None)
        return result


def create_client_from_settings(settings: Settings) -> GmailClient | None:
    """Build a Gmail client when Gmail is configured, else return ``None``.

    When the credential paths are missing the application must keep working and
    simply skip all Gmail work. When they are configured but the token file is
    missing/invalid, a :class:`GmailAuthError` is raised to the caller.
    """

    if not settings.gmail_enabled:
        return None
    credentials = load_credentials(settings.gmail_token_file)
    return GmailClient(credentials)
