"""Gmail message importer: Gmail API -> whitelist -> parser -> Inbox.

The importer is an orchestration service: it owns the database commits (one
short transaction per message, never held across a Gmail API call) so that one
malformed message cannot roll back previously imported ones. It uses the
existing idempotent ``app.inbox`` service, so a repeated run never creates
duplicate ``InboxMessage`` rows.

It intentionally does NOT call DeepSeek, Google Sheets, or Telegram — those are
later stages.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app import inbox, senders
from app.gmail.client import GmailClient, GmailError
from app.gmail.parsing import (
    extract_body,
    extract_email,
    get_header,
    internal_date_to_datetime,
    normalize_email,
)

logger = logging.getLogger(__name__)

# The permitted senders live in the database (the "Email" sheet edits them).
# They are compared against the normalized email address with exact,
# case-insensitive match (never by substring or domain). Each import run reads
# the current list, so a change in the sheet applies to the next run.


def build_query(allowed: Collection[str]) -> str:
    """Server-side Gmail search listing only the permitted senders, so the rest of
    the mailbox is never fetched. The addresses are validated (a narrow alphabet)
    before they get into the list, so they cannot alter the query."""

    return "from:(" + " OR ".join(sorted(allowed)) + ")"

_GMAIL_PAGE_SIZE = 500  # Gmail API max results per page


@dataclass
class ImportStats:
    """Counters for one Gmail import run."""

    found: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_not_allowed: int = 0
    failed: int = 0


def is_allowed_sender(email: str | None, allowed: Collection[str]) -> bool:
    """Return ``True`` when ``email`` is in the sender whitelist ``allowed``."""

    if not email:
        return False
    return normalize_email(email) in allowed


async def import_messages(
    session: AsyncSession,
    client: GmailClient,
    *,
    max_messages: int | None = None,
    query: str | None = None,
) -> ImportStats:
    """Import whitelisted Gmail messages into the inbox.

    ``max_messages`` caps how many messages are examined per run (the caller
    passes ``settings.gmail_max_messages``). ``query`` is the Gmail search
    expression; it defaults to the active senders from the database so the rest
    of the mailbox is never fetched. It is only a server-side filter: the sender
    whitelist is always re-checked in Python afterwards. With no active sender
    nothing is fetched at all.

    The caller should provide a dedicated session: this function commits each
    imported message individually.
    """

    stats = ImportStats()
    allowed = await senders.active_emails(session)
    await session.commit()  # do not hold the read transaction across Gmail calls
    if not allowed:
        logger.info("Gmail import skipped: no active senders in the Email sheet")
        return stats
    if query is None:
        query = build_query(allowed)
    page_token: str | None = None

    while True:
        remaining: int | None = None
        if max_messages is not None:
            remaining = max_messages - stats.found
            if remaining <= 0:
                break
        page_size = (
            min(_GMAIL_PAGE_SIZE, remaining) if remaining is not None else _GMAIL_PAGE_SIZE
        )

        page = await client.list_messages(
            q=query, max_results=page_size, page_token=page_token
        )
        # Enforce the cap here too instead of trusting the API to honour it.
        message_ids = page.messages if remaining is None else page.messages[:remaining]
        for message_id in message_ids:
            stats.found += 1
            await _import_one(session, client, message_id, stats, allowed)

        if page.next_page_token is None:
            break
        page_token = page.next_page_token

    return stats


async def _import_one(
    session: AsyncSession,
    client: GmailClient,
    message_id: str,
    stats: ImportStats,
    allowed: Collection[str],
) -> None:
    # Already imported messages are skipped without downloading them again.
    existing = await inbox.get_by_external_id(session, "gmail", message_id)
    # End the read transaction so it is not held across the Gmail API call.
    await session.commit()
    if existing is not None:
        stats.duplicates += 1
        logger.info("Duplicate Gmail message: %s", message_id)
        return

    try:
        message = await client.get_message(message_id)
    except GmailError as exc:
        if exc.http_status in (401, 403):
            # An auth/scope failure affects the whole import, not just this one
            # message, so surface it instead of silently failing every message.
            raise
        stats.failed += 1
        logger.warning("Failed to fetch Gmail message %s: %s", message_id, exc.message)
        return

    try:
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []

        sender = normalize_email(extract_email(get_header(headers, "From")))
        if not is_allowed_sender(sender, allowed):
            stats.skipped_not_allowed += 1
            logger.info("Skipped Gmail message %s from a non-whitelisted sender", message_id)
            return

        subject = get_header(headers, "Subject") or ""
        body = extract_body(payload)
        received_at = internal_date_to_datetime(message.get("internalDate"))
    except (AttributeError, TypeError, ValueError) as exc:
        # A structurally malformed message must not abort the rest of the batch.
        stats.failed += 1
        logger.warning(
            "Failed to parse Gmail message %s: %s", message_id, type(exc).__name__
        )
        return

    await inbox.create_message(
        session,
        source="gmail",
        external_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
    )
    await session.commit()
    stats.imported += 1
    logger.info("Imported Gmail message: %s", message_id)
