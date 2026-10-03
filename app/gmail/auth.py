"""Gmail OAuth credential loading and refresh.

This layer is deliberately separate from the importer: it only deals with
obtaining valid credentials. It never starts an interactive browser OAuth flow
— the one-time authorization is done out-of-band and only its token file is
consumed here. If a token is missing or cannot be refreshed, a clear
:class:`GmailAuthError` is raised so the caller (the importer) can report it
without crashing the rest of the application.

The one-time authorization itself is :func:`authorize`, run manually with
``python -m app.gmail``; it is the only code that reads the OAuth client
secret file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthError(Exception):
    """Raised when Gmail credentials are missing or cannot be obtained."""

    def __init__(self, *, message: str = "Gmail authentication failed") -> None:
        self.message = message
        super().__init__(message)


def save_token(credentials: Any, token_file: str) -> None:
    """Persist credentials to the token file atomically.

    The JSON is written to a temporary file next to the target and then moved
    into place, so a crash mid-write never leaves a truncated token file. The
    file holds a refresh token, so it is created owner-only (0600) regardless
    of the process umask — ``os.replace`` would otherwise give the token file
    the temp file's default, typically world-readable, permissions.
    """

    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)  # a leftover could carry looser permissions
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())
    os.chmod(tmp_path, 0o600)  # the mode argument is still subject to umask
    os.replace(tmp_path, path)


def load_credentials(token_file: str) -> Any:
    """Load Gmail credentials from ``token_file``, refreshing if expired.

    Returns valid credentials. Raises :class:`GmailAuthError` when the token
    file is absent, the credentials cannot be refreshed, or they are invalid.
    """

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not Path(token_file).exists():
        raise GmailAuthError(
            message=(
                "Gmail token file not found; the one-time authorization has "
                "not been completed yet"
            )
        )

    try:
        credentials = Credentials.from_authorized_user_file(token_file, SCOPES)
    except (ValueError, OSError, AttributeError) as exc:
        raise GmailAuthError(
            message=(
                "Gmail token file is malformed or unreadable; re-authorize and "
                "regenerate the token file"
            )
        ) from exc

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - wrap any refresh failure
            logger.warning("Gmail token refresh failed")
            raise GmailAuthError(message="Gmail token refresh failed") from exc
        try:
            save_token(credentials, token_file)
        except OSError as exc:
            # The refreshed credentials are valid in memory, so this run can
            # continue; the next run simply refreshes again.
            logger.warning(
                "Could not save refreshed Gmail token: %s", type(exc).__name__
            )
        return credentials

    raise GmailAuthError(
        message=(
            "Gmail credentials are invalid and cannot be refreshed; "
            "re-authorize and regenerate the token file"
        )
    )


def authorize(client_secret_file: str, token_file: str) -> None:
    """Run the one-time interactive OAuth consent and save the token.

    This is the out-of-band step that produces ``token_file``. It opens a
    browser, so it must only be run manually (``python -m app.gmail``) and
    is never called by the running application.
    """

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    credentials = flow.run_local_server(port=0)
    save_token(credentials, token_file)


def main() -> None:
    """CLI entry point for the one-time Gmail authorization."""

    from app.config import get_settings

    settings = get_settings()
    if not settings.gmail_enabled:
        raise SystemExit("Set GMAIL_CLIENT_SECRET_FILE and GMAIL_TOKEN_FILE first")
    authorize(settings.gmail_client_secret_file, settings.gmail_token_file)
    print("Gmail token saved")
