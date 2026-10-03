"""Tests for Gmail parsing helpers and the sender whitelist."""

from __future__ import annotations

import base64
from datetime import datetime

import pytest

from app.gmail.importer import is_allowed_sender
from app.gmail.parsing import (
    decode_base64url,
    extract_body,
    extract_email,
    get_header,
    html_to_text,
    internal_date_to_datetime,
    normalize_email,
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- base64url decoding ------------------------------------------------------


def test_decode_base64url_roundtrip() -> None:
    assert decode_base64url(_b64url("Hello, world")) == "Hello, world"


def test_decode_base64url_empty_and_none() -> None:
    assert decode_base64url(None) == ""
    assert decode_base64url("") == ""


def test_decode_base64url_malformed_returns_empty() -> None:
    assert decode_base64url("!!!not-base64!!!") == ""


# --- internalDate ------------------------------------------------------------


def test_internal_date_to_datetime() -> None:
    result = internal_date_to_datetime("1609459200000")
    assert result == datetime(2021, 1, 1, 0, 0, 0)
    assert result.tzinfo is None


@pytest.mark.parametrize("value", [None, "", "not-a-number"])
def test_internal_date_invalid_returns_none(value: str | None) -> None:
    assert internal_date_to_datetime(value) is None


# --- HTML to text ------------------------------------------------------------


def test_html_to_text_basic() -> None:
    html = "<html><body><h1>Hello</h1><p>Test</p></body></html>"
    assert "Hello" in html_to_text(html)
    assert "Test" in html_to_text(html)


def test_html_to_text_empty() -> None:
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_html_to_text_ignores_script_and_style() -> None:
    html = (
        "<p>Hello</p>"
        '<script>alert("secret");</script>'
        "<style>body { display:none }</style>"
        "<p>World</p>"
    )
    text = html_to_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "alert" not in text
    assert "secret" not in text
    assert "display" not in text


# --- headers -----------------------------------------------------------------


def test_get_header_case_insensitive() -> None:
    headers = [{"name": "fRoM", "value": "a@b.c"}, {"name": "Subject", "value": "Hi"}]
    assert get_header(headers, "From") == "a@b.c"
    assert get_header(headers, "FROM") == "a@b.c"
    assert get_header(headers, "subject") == "Hi"


def test_get_header_missing_returns_none() -> None:
    assert get_header([], "Subject") is None
    assert get_header(None, "Subject") is None


# --- email extraction / normalization ---------------------------------------


def test_extract_email_from_display_name() -> None:
    assert extract_email("LiteServer B.V. <support@liteserver.nl>") == "support@liteserver.nl"


def test_extract_email_bare() -> None:
    assert extract_email("admin@ztv.su") == "admin@ztv.su"


def test_extract_email_empty() -> None:
    assert extract_email(None) is None
    assert extract_email("") is None


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email("  Support@LiteServer.NL ") == "support@liteserver.nl"


# --- whitelist ---------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "support@liteserver.nl",
        "admin@ztv.su",
        "SUPPORT@LITESERVER.NL",
        "  admin@ztv.su  ",
    ],
)
def test_is_allowed_sender_allows(email: str) -> None:
    assert is_allowed_sender(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "unknown@example.com",
        "support@liteserver.ru",
        "support@liteserver.nl.attacker.com",
        "support+lorem@liteserver.nl",
        "support@liteserver.nlx",
        "liteserver.nl",
    ],
)
def test_is_allowed_sender_denies(email: str) -> None:
    assert is_allowed_sender(email) is False


def test_is_allowed_sender_denies_none() -> None:
    assert is_allowed_sender(None) is False


# --- body extraction ---------------------------------------------------------


def test_extract_body_plain() -> None:
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64url("Plain body")},
    }
    assert extract_body(payload) == "Plain body"


def test_extract_body_html_only() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64url("<html><body><p>HTML body</p></body></html>")},
    }
    body = extract_body(payload)
    assert body is not None
    assert "HTML body" in body


def test_extract_body_prefers_plain_over_html() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("Plain wins")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML ignored</p>")}},
        ],
    }
    assert extract_body(payload) == "Plain wins"


def test_extract_body_nested_multipart() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/related",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("Nested text")}}
                ],
            }
        ],
    }
    assert extract_body(payload) == "Nested text"


def test_extract_body_ignores_attachments() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("Real body")}},
            {
                "mimeType": "application/pdf",
                "filename": "x.pdf",
                "body": {"data": _b64url("binary junk")},
            },
        ],
    }
    assert extract_body(payload) == "Real body"


def test_extract_body_empty_or_missing() -> None:
    assert extract_body(None) is None
    assert extract_body({"mimeType": "text/plain", "body": {}}) is None
    assert extract_body({"mimeType": "multipart/alternative", "parts": []}) is None


# --- review fixes ------------------------------------------------------------


def test_html_to_text_keeps_trailing_text_with_ampersand() -> None:
    assert html_to_text("<p>Thanks</p>AT&T") == "Thanks\nAT&T"


def test_html_to_text_drops_title_and_separates_cells() -> None:
    html = (
        "<html><head><title>Invoice</title></head><body>"
        "<table><tr><td>Total</td><td>10 EUR</td></tr></table></body></html>"
    )
    assert html_to_text(html) == "Total 10 EUR"


def test_extract_body_uses_part_charset() -> None:
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="windows-1251"'}],
        "body": {"data": base64.urlsafe_b64encode("Привет".encode("cp1251")).decode()},
    }
    assert extract_body(payload) == "Привет"


def test_extract_body_unknown_charset_falls_back_to_utf8() -> None:
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=x-unknown"}],
        "body": {"data": _b64url("Hello")},
    }
    assert extract_body(payload) == "Hello"


def test_extract_body_skips_text_attachments() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("Main body")}},
            {
                "mimeType": "text/plain",
                "filename": "notes.txt",
                "body": {"data": _b64url("ATTACHMENT")},
            },
            {
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                "body": {"data": _b64url("ATTACHMENT 2")},
            },
        ],
    }
    assert extract_body(payload) == "Main body"


def test_extract_body_ignores_malformed_parts() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": ["junk", {"mimeType": "text/plain", "body": "not-a-dict"},
                  {"mimeType": "text/plain", "body": {"data": 123}},
                  {"mimeType": "text/plain", "body": {"data": _b64url("ok")}}],
    }
    assert extract_body(payload) == "ok"


def test_internal_date_out_of_range_returns_none() -> None:
    assert internal_date_to_datetime("99999999999999999999") is None


# --- sender extraction security (display-name spoofing) ------------------------------
# The whitelist is checked against extract_email(From). Only the structured
# RFC 5322 parse may produce that address; nothing is guessed from free text.


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("John Doe <john@example.com>", "john@example.com"),
        ("<john@example.com>", "john@example.com"),
        ("john@example.com", "john@example.com"),
        ('"Support, Team" <support@liteserver.nl>', "support@liteserver.nl"),
        ("Поддержка LiteServer <support@liteserver.nl>", "support@liteserver.nl"),
        ("=?utf-8?b?0J/QvtC00LTQtdGA0LbQutCw?= <support@liteserver.nl>", "support@liteserver.nl"),
        ("admin@ztv.su (ZTV admin)", "admin@ztv.su"),
        (" admin@ztv.su \r\n", "admin@ztv.su"),
    ],
)
def test_extract_email_keeps_legitimate_rfc5322_forms(header: str, expected: str) -> None:
    assert extract_email(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        # Allowed address in an unquoted display name, real mailbox is the attacker's.
        "support@liteserver.nl <attacker@evil.example>",
        "Fake support@liteserver.nl Name <attacker@evil.example>",
        # Several senders: ambiguous, never pick the first one.
        "support@liteserver.nl, attacker@evil.example",
        "support@liteserver.nl;attacker@evil.example",
        # Malformed headers: no guessing from the text.
        "support@liteserver.nl>evil",
        "support@liteserver.nl <>",
        "garbage text support@liteserver.nl more garbage",
        "not an address at all",
    ],
)
def test_extract_email_never_guesses_from_free_text(header: str) -> None:
    address = extract_email(header)
    assert address != "support@liteserver.nl"
    assert address is None
    assert is_allowed_sender(normalize_email(address)) is False


def test_quoted_display_name_with_allowed_address_resolves_to_real_mailbox() -> None:
    header = '"support@liteserver.nl" <attacker@evil.example>'
    assert extract_email(header) == "attacker@evil.example"
    assert is_allowed_sender(normalize_email(extract_email(header))) is False


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("Support <support@liteserver.nl>", True),
        ("Attacker <attacker@evil.example>", False),
        ("Fake Name <attacker@evil.example>", False),
        ("support@liteserver.nl <attacker@evil.example>", False),
    ],
)
def test_whitelist_decision_uses_parsed_sender_only(header: str, allowed: bool) -> None:
    assert is_allowed_sender(normalize_email(extract_email(header))) is allowed
