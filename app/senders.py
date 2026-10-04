"""Allowed Gmail senders: validation and deterministic domain operations.

The Gmail import reads only messages from these addresses, so an address must be
exactly what the sender really uses: one complete ``name@domain.tld`` mailbox,
compared case-insensitively and never by domain or substring. Anything else is
rejected with an explanation instead of being corrected by guesswork.

Like the other domain modules, functions flush but never commit.
"""

from __future__ import annotations

import re
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.allowed_sender import AllowedSender

# Letters, digits and . _ % + - in the local part; a dotted domain with a 2+ letter ending.
# The narrow alphabet also keeps the address safe to put into a Gmail search query.
_ADDRESS_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")
MAX_LENGTH = 254


def normalize_sender(value: object) -> str:
    """Return the canonical form of an address typed by the user, or raise ``ValueError``.

    Accepts a plain address, ``mailto:`` and the ``Name <address>`` form copied
    from a mail client. Rejects several addresses in one cell, a bare domain
    (``@example.com``, ``example.com``) and anything malformed.
    """

    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError("Email address is empty")
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:"):].strip()
    mailboxes = getaddresses([text])
    address = mailboxes[0][1] if len(mailboxes) == 1 else None
    if not address:
        raise ValueError(
            f"Expected exactly one email address like name@example.com, got {text!r} "
            "(one address per row; a whole domain is not allowed)"
        )
    address = address.strip().lower()
    if len(address) > MAX_LENGTH or not _ADDRESS_RE.match(address):
        raise ValueError(f"Not a valid email address: {text!r} (expected name@example.com)")
    return address


async def _find_by_email(session: AsyncSession, email: str) -> AllowedSender | None:
    result = await session.execute(select(AllowedSender).where(AllowedSender.email == email))
    return result.scalar_one_or_none()


async def create_sender(
    session: AsyncSession, *, email: object, is_active: bool = True
) -> AllowedSender:
    address = normalize_sender(email)
    if await _find_by_email(session, address) is not None:
        raise ValueError(f"This address is already in the list: {address}")
    sender = AllowedSender(email=address, is_active=is_active)
    session.add(sender)
    await session.flush()
    return sender


async def update_sender(
    session: AsyncSession,
    sender: AllowedSender,
    *,
    email: object | None = None,
    is_active: bool | None = None,
) -> AllowedSender:
    if email is not None:
        address = normalize_sender(email)
        other = await _find_by_email(session, address)
        if other is not None and other.id != sender.id:
            raise ValueError(f"This address is already in the list: {address}")
        sender.email = address
    if is_active is not None:
        sender.is_active = is_active
    await session.flush()
    return sender


async def delete_sender(session: AsyncSession, sender: AllowedSender) -> None:
    await session.delete(sender)
    await session.flush()


async def list_senders(session: AsyncSession) -> list[AllowedSender]:
    result = await session.execute(select(AllowedSender).order_by(AllowedSender.id))
    return list(result.scalars())


async def active_emails(session: AsyncSession) -> frozenset[str]:
    """The addresses the Gmail import may read right now."""

    result = await session.execute(
        select(AllowedSender.email).where(AllowedSender.is_active.is_(True))
    )
    return frozenset(result.scalars())
