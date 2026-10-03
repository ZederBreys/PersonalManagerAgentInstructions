"""Pure parsing helpers for Gmail messages.

These functions turn a raw Gmail ``messages.get`` payload into the fields the
inbox needs (``sender``, ``subject``, ``body``, ``received_at``). They are pure
and synchronous so they are trivial to unit-test, and they never perform any
network I/O.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

# Match a bare email address; used only as a fallback when ``parseaddr`` cannot
# extract an address. The whitelist does an exact-match afterwards, so a false
# extraction here can only result in "not allowed", never in a wrong allow.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def get_header(headers: list[dict[str, str]] | None, name: str) -> str | None:
    """Return the value of the first header matching ``name`` (case-insensitive)."""

    if not headers:
        return None
    wanted = name.lower()
    for header in headers:
        if (header.get("name") or "").lower() == wanted:
            return header.get("value")
    return None


def extract_email(from_header: str | None) -> str | None:
    """Extract the bare email address from a ``From`` header value."""

    if not from_header:
        return None
    _, address = parseaddr(from_header)
    if address:
        return address
    match = _EMAIL_RE.search(from_header)
    return match.group(0) if match else None


def normalize_email(address: str | None) -> str | None:
    """Normalize an email address for whitelist comparison (trim + lowercase)."""

    if not address:
        return None
    return address.strip().lower()


def decode_base64url(data: str | None, charset: str = "utf-8") -> str:
    """Decode Gmail base64url body data into text using ``charset``.

    Handles missing padding, empty input, malformed data and unknown charsets
    (falling back to UTF-8) without raising. Invalid bytes are replaced.
    """

    if not data or not isinstance(data, str):
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
    except ValueError:
        return ""
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def internal_date_to_datetime(internal_date: str | None) -> datetime | None:
    """Convert Gmail ``internalDate`` (epoch milliseconds) to naive UTC datetime.

    The project-wide convention is naive UTC timestamps (see ``app.db.utcnow``).
    """

    if internal_date is None:
        return None
    try:
        millis = int(internal_date)
        moment = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.replace(tzinfo=None)


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text converter using only the standard library.

    Strips tags and collapses the result into readable lines. Common block-level
    elements introduce line breaks, table cells are separated by a space, and
    non-content elements (scripts, styles, the document title) are dropped.
    """

    _SKIP_TAGS = {"script", "style", "title"}
    _CELL_TAGS = {"td", "th"}

    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "blockquote",
        "pre",
        "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag in self._CELL_TAGS:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag in self._CELL_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str | None) -> str:
    """Convert an HTML fragment to plain readable text (no external deps)."""

    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    # ``close`` flushes text still buffered by the parser (e.g. a trailing
    # "AT&T" that could be the start of a character reference).
    parser.close()
    return parser.text()


def _is_attachment(part: dict[str, Any]) -> bool:
    """Return ``True`` for parts that are attachments rather than the body."""

    if part.get("filename"):
        return True
    disposition = get_header(part.get("headers"), "Content-Disposition") or ""
    return disposition.strip().lower().startswith("attachment")


def _part_charset(part: dict[str, Any]) -> str:
    """Return the charset declared in the part's ``Content-Type`` (UTF-8 default)."""

    content_type = get_header(part.get("headers"), "Content-Type")
    if not content_type:
        return "utf-8"
    message = Message()
    message["Content-Type"] = content_type
    return message.get_content_charset() or "utf-8"


def _iter_text_parts(payload: dict[str, Any] | None) -> Any:
    """Yield ``(mime_type, part)`` for every leaf text body part in the MIME tree."""

    if not isinstance(payload, dict):
        return
    mime_type = (payload.get("mimeType") or "").lower()
    if mime_type in ("text/plain", "text/html"):
        if not _is_attachment(payload):
            yield mime_type, payload
        return
    for part in payload.get("parts", []) or []:
        yield from _iter_text_parts(part)


def extract_body(payload: dict[str, Any] | None) -> str | None:
    """Extract the readable text body from a Gmail payload.

    Recursively walks ``multipart/*`` trees and prefers ``text/plain`` over
    ``text/html``. Attachments (including text ones) are ignored, and each part
    is decoded with the charset from its ``Content-Type``.
    """

    plain: list[str] = []
    html: list[str] = []
    for mime_type, part in _iter_text_parts(payload):
        body = part.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if not data:
            continue
        text = decode_base64url(data, _part_charset(part))
        if not text:
            continue
        if mime_type == "text/plain":
            plain.append(text)
        else:
            html.append(html_to_text(text))
    if plain:
        return "\n\n".join(plain)
    if html:
        return "\n\n".join(html)
    return None
