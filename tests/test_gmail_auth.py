"""Tests for Gmail OAuth credential loading and refresh."""

from __future__ import annotations

import pytest

from app.gmail.auth import SCOPES, GmailAuthError, authorize, load_credentials, save_token


class FakeCredentials:
    valid = True
    expired = False
    refresh_token = "refresh-token"
    refreshed = False
    to_json_calls = 0

    def refresh(self, request) -> None:
        self.refreshed = True
        self.valid = True

    def to_json(self) -> str:
        type(self).to_json_calls += 1
        return '{"token": "refreshed"}'

    @classmethod
    def from_authorized_user_file(cls, token_file, scopes):
        return cls()


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch):
    # Tests mutate class attributes; registering them with monkeypatch restores
    # the defaults afterwards so no state leaks between tests.
    for name in ("valid", "expired", "refresh_token", "refreshed", "to_json_calls", "refresh"):
        monkeypatch.setattr(FakeCredentials, name, FakeCredentials.__dict__[name])
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials", FakeCredentials
    )
    return FakeCredentials


def test_load_credentials_valid(tmp_path, fake_credentials) -> None:
    FakeCredentials.valid = True
    FakeCredentials.expired = False
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    credentials = load_credentials(str(token_file))
    assert credentials is not None


def test_load_credentials_expired_refreshes_and_saves(tmp_path, fake_credentials) -> None:
    FakeCredentials.valid = False
    FakeCredentials.expired = True
    FakeCredentials.refresh_token = "refresh-token"
    FakeCredentials.refreshed = False
    FakeCredentials.to_json_calls = 0
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    credentials = load_credentials(str(token_file))
    assert credentials.refreshed is True
    assert FakeCredentials.to_json_calls == 1
    assert "refreshed" in token_file.read_text()


def test_load_credentials_missing_token(tmp_path, fake_credentials) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(GmailAuthError, match="not been completed"):
        load_credentials(str(missing))


def test_load_credentials_expired_without_refresh_token(
    tmp_path, fake_credentials
) -> None:
    FakeCredentials.valid = False
    FakeCredentials.expired = True
    FakeCredentials.refresh_token = None
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    with pytest.raises(GmailAuthError, match="cannot be refreshed"):
        load_credentials(str(token_file))


def test_load_credentials_invalid_not_expired(tmp_path, fake_credentials) -> None:
    FakeCredentials.valid = False
    FakeCredentials.expired = False
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    with pytest.raises(GmailAuthError, match="cannot be refreshed"):
        load_credentials(str(token_file))


def test_load_credentials_refresh_failure(tmp_path, fake_credentials) -> None:
    def boom(request):
        raise RuntimeError("refresh exploded")

    FakeCredentials.valid = False
    FakeCredentials.expired = True
    FakeCredentials.refresh_token = "refresh-token"
    FakeCredentials.refresh = boom
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    with pytest.raises(GmailAuthError, match="refresh failed"):
        load_credentials(str(token_file))


def test_load_credentials_malformed_json(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text("not valid json")

    with pytest.raises(GmailAuthError, match="malformed"):
        load_credentials(str(token_file))


def test_load_credentials_empty_file(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text("")

    with pytest.raises(GmailAuthError, match="malformed"):
        load_credentials(str(token_file))


def test_load_credentials_invalid_structure(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text('{"client_id": "x", "client_secret": "y"}')

    with pytest.raises(GmailAuthError, match="malformed"):
        load_credentials(str(token_file))


def test_load_credentials_save_failure_still_returns_credentials(
    tmp_path, fake_credentials, monkeypatch
) -> None:
    FakeCredentials.valid = False
    FakeCredentials.expired = True
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "x"}')

    def fail(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr("app.gmail.auth.save_token", fail)
    credentials = load_credentials(str(token_file))
    assert credentials.refreshed is True
    assert token_file.read_text() == '{"token": "x"}'


def test_save_token_replaces_file_atomically(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text("old")

    save_token(FakeCredentials(), str(token_file))
    assert token_file.read_text() == '{"token": "refreshed"}'
    assert list(tmp_path.iterdir()) == [token_file]


def test_authorize_runs_flow_and_saves_token(tmp_path, monkeypatch) -> None:
    calls: dict = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            calls["path"], calls["scopes"] = path, scopes
            return cls()

        def run_local_server(self, port):
            return FakeCredentials()

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", FakeFlow)
    token_file = tmp_path / "token.json"

    authorize("client_secret.json", str(token_file))
    assert calls == {"path": "client_secret.json", "scopes": SCOPES}
    assert token_file.read_text() == '{"token": "refreshed"}'
