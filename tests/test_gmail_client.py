"""Tests for the Gmail API client wrapper (fake service, no real Google)."""

from __future__ import annotations

import asyncio
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.gmail.client import GmailClient, GmailError, MessageList


def _http_error(status: int, message: str) -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    content = json.dumps({"error": {"message": message}}).encode()
    return HttpError(resp, content)


class _Request:
    def __init__(self, result, error) -> None:
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeMessages:
    def __init__(self, *, list_result=None, list_error=None, get_results=None, get_error=None) -> None:
        self.list_kwargs = None
        self.list_result = list_result
        self.list_error = list_error
        self.get_kwargs: list[dict] = []
        self.get_results = get_results or {}
        self.get_error = get_error

    def list(self, **kwargs) -> _Request:
        self.list_kwargs = kwargs
        return _Request(self.list_result, self.list_error)

    def get(self, **kwargs) -> _Request:
        self.get_kwargs.append(kwargs)
        error = self.get_error
        return _Request(self.get_results.get(kwargs["id"]), error)


class _FakeUsers:
    def __init__(self, messages) -> None:
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, messages) -> None:
        self._messages = messages

    def users(self):
        return _FakeUsers(self._messages)


def _client(messages) -> GmailClient:
    return GmailClient(credentials=None, service=_FakeService(messages))


def test_list_messages_returns_ids_and_token() -> None:
    messages = _FakeMessages(
        list_result={"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "tok"}
    )
    result = asyncio.run(_client(messages).list_messages())
    assert result.messages == ["a", "b"]
    assert result.next_page_token == "tok"
    assert messages.list_kwargs["userId"] == "me"


def test_list_messages_passes_query_max_results_page_token() -> None:
    messages = _FakeMessages(list_result={})
    asyncio.run(
        _client(messages).list_messages(q="from:x@y.z", max_results=50, page_token="pt")
    )
    assert messages.list_kwargs == {
        "userId": "me",
        "q": "from:x@y.z",
        "maxResults": 50,
        "pageToken": "pt",
    }


def test_list_messages_empty_result() -> None:
    messages = _FakeMessages(list_result={})
    result = asyncio.run(_client(messages).list_messages())
    assert result.messages == []
    assert result.next_page_token is None


@pytest.mark.parametrize("bad_id", [None, "", 123, ["x"]])
def test_list_messages_rejects_malformed_id(bad_id) -> None:
    item = {} if bad_id is None else {"id": bad_id}
    messages = _FakeMessages(list_result={"messages": [{"id": "a"}, item]})
    with pytest.raises(GmailError, match="Malformed"):
        asyncio.run(_client(messages).list_messages())


def test_list_messages_rejects_non_string_page_token() -> None:
    messages = _FakeMessages(list_result={"messages": [], "nextPageToken": 5})
    with pytest.raises(GmailError, match="Malformed"):
        asyncio.run(_client(messages).list_messages())


def test_list_messages_empty_page_token_means_last_page() -> None:
    messages = _FakeMessages(list_result={"messages": [{"id": "a"}], "nextPageToken": ""})
    result = asyncio.run(_client(messages).list_messages())
    assert result.next_page_token is None


def test_get_message_passes_id_and_format() -> None:
    messages = _FakeMessages(get_results={"abc": {"id": "abc", "internalDate": "1"}})
    result = asyncio.run(_client(messages).get_message("abc"))
    assert result == {"id": "abc", "internalDate": "1"}
    assert messages.get_kwargs == [{"userId": "me", "id": "abc", "format": "full"}]


def test_list_messages_raises_gmail_error_on_http_error() -> None:
    messages = _FakeMessages(list_error=_http_error(500, "boom"))
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).list_messages())
    assert exc_info.value.http_status == 500
    assert "boom" in str(exc_info.value)


def test_get_message_raises_gmail_error_on_http_error() -> None:
    messages = _FakeMessages(get_error=_http_error(403, "forbidden"))
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).get_message("abc"))
    assert exc_info.value.http_status == 403
    assert "forbidden" in str(exc_info.value)


def test_list_messages_wraps_transport_error() -> None:
    messages = _FakeMessages(list_error=httplib2.HttpLib2Error("network down"))
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).list_messages())
    assert exc_info.value.http_status is None
    assert "transport" in str(exc_info.value)


def test_get_message_wraps_transport_error() -> None:
    messages = _FakeMessages(get_error=httplib2.HttpLib2Error("network down"))
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).get_message("abc"))
    assert exc_info.value.http_status is None
    assert "transport" in str(exc_info.value)


def test_list_messages_malformed_response_not_dict() -> None:
    messages = _FakeMessages(list_result="not-a-dict")
    with pytest.raises(GmailError, match="Malformed"):
        asyncio.run(_client(messages).list_messages())


def test_list_messages_malformed_messages_not_list() -> None:
    messages = _FakeMessages(list_result={"messages": "not-a-list"})
    with pytest.raises(GmailError, match="Malformed"):
        asyncio.run(_client(messages).list_messages())


def test_get_message_malformed_response_not_dict() -> None:
    messages = _FakeMessages(get_results={"abc": "not-a-dict"})
    with pytest.raises(GmailError, match="Malformed"):
        asyncio.run(_client(messages).get_message("abc"))


@pytest.mark.parametrize(
    "error", [TimeoutError("timed out"), ConnectionResetError("reset")]
)
def test_list_messages_wraps_socket_error(error: OSError) -> None:
    messages = _FakeMessages(list_error=error)
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).list_messages())
    assert exc_info.value.http_status is None
    assert "transport" in str(exc_info.value)


@pytest.mark.parametrize(
    "error", [TimeoutError("timed out"), ConnectionResetError("reset")]
)
def test_get_message_wraps_socket_error(error: OSError) -> None:
    messages = _FakeMessages(get_error=error)
    with pytest.raises(GmailError) as exc_info:
        asyncio.run(_client(messages).get_message("abc"))
    assert exc_info.value.http_status is None
    assert "transport" in str(exc_info.value)
