"""Gmail integration: read-only message import into the inbox."""

from __future__ import annotations

from app.gmail.auth import GmailAuthError, load_credentials
from app.gmail.client import GmailClient, GmailError, MessageList, create_client_from_settings
from app.gmail.importer import (
    ALLOWED_SENDERS,
    ImportStats,
    import_messages,
    is_allowed_sender,
)
from app.gmail.parsing import (
    decode_base64url,
    extract_body,
    extract_email,
    get_header,
    html_to_text,
    internal_date_to_datetime,
    normalize_email,
)

__all__ = [
    "ALLOWED_SENDERS",
    "GmailAuthError",
    "GmailClient",
    "GmailError",
    "ImportStats",
    "MessageList",
    "create_client_from_settings",
    "decode_base64url",
    "extract_body",
    "extract_email",
    "get_header",
    "html_to_text",
    "import_messages",
    "internal_date_to_datetime",
    "is_allowed_sender",
    "load_credentials",
    "normalize_email",
]
