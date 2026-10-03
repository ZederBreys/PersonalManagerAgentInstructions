"""Tests for Google Sheets configuration gating."""

from __future__ import annotations

from app.config import Settings
from app.google_sheets.client import GoogleSheetsClient, create_client_from_settings


def test_sheets_disabled_without_credentials() -> None:
    settings = Settings()
    assert settings.google_sheets_enabled is False
    assert create_client_from_settings(settings) is None


def test_sheets_disabled_without_spreadsheet_id() -> None:
    settings = Settings(google_service_account_file="creds.json")
    assert settings.google_sheets_enabled is False
    assert create_client_from_settings(settings) is None


def test_sheets_enabled_when_configured() -> None:
    settings = Settings(
        google_service_account_file="creds.json", google_spreadsheet_id="spreadsheet-id"
    )
    assert settings.google_sheets_enabled is True
    client = create_client_from_settings(settings)
    assert isinstance(client, GoogleSheetsClient)


def test_credentials_not_in_repr_or_settings_defaults() -> None:
    settings = Settings(
        google_service_account_file="/secret/creds.json", google_spreadsheet_id="abc"
    )
    client = create_client_from_settings(settings)
    assert "creds.json" not in repr(client)
