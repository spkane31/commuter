from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from commuter.app import create_app
from commuter.config import Settings
from commuter.models import Athlete, TokenSet
from commuter.store import CredentialStore


@dataclass
class FakeStravaClient:
    exchanged_code: str | None = None
    refreshed_token: str | None = None
    revoked_token: str | None = None

    async def exchange_authorization_code(self, code: str) -> TokenSet:
        self.exchanged_code = code
        return TokenSet(
            access_token="fresh-access-token",
            refresh_token="fresh-refresh-token",
            expires_at=2_000_000_000,
            athlete=Athlete(id=123, username="commuter"),
        )

    async def refresh_access_token(self, refresh_token: str) -> TokenSet:
        self.refreshed_token = refresh_token
        return TokenSet(
            access_token="refreshed-access-token",
            refresh_token="rotated-refresh-token",
            expires_at=2_000_000_100,
            athlete=Athlete(id=123, username="commuter"),
        )

    async def revoke(self, refresh_token: str) -> None:
        self.revoked_token = refresh_token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        base_url="http://testserver",
    )


@pytest.fixture
def fake_strava() -> FakeStravaClient:
    return FakeStravaClient()


@pytest.fixture
def client(settings: Settings, fake_strava: FakeStravaClient) -> TestClient:
    app = create_app(settings=settings, strava_client=fake_strava)
    return TestClient(app)


def begin_authorization(client: TestClient) -> str:
    response = client.get("/auth/strava", follow_redirects=False)

    assert response.status_code == 303
    authorization_url = urlparse(response.headers["location"])
    query = parse_qs(authorization_url.query)
    assert authorization_url.scheme == "https"
    assert authorization_url.netloc == "www.strava.com"
    assert authorization_url.path == "/oauth/authorize"
    assert query["client_id"] == ["12345"]
    assert query["redirect_uri"] == ["http://testserver/auth/strava/callback"]
    assert set(query["scope"][0].split(",")) == {"activity:read_all", "activity:write"}
    return query["state"][0]


def test_oauth_callback_persists_plaintext_credentials_with_owner_only_database_permissions(
    client: TestClient,
    settings: Settings,
    fake_strava: FakeStravaClient,
) -> None:
    state = begin_authorization(client)

    response = client.get(
        "/auth/strava/callback",
        params={
            "code": "authorization-code",
            "state": state,
            "scope": "activity:read_all,activity:write",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake_strava.exchanged_code == "authorization-code"

    store = CredentialStore(settings.database_path)
    account = store.get_account(123)
    assert account is not None
    assert account.access_token == "fresh-access-token"
    assert account.refresh_token == "fresh-refresh-token"
    assert account.scopes == {"activity:read_all", "activity:write"}

    database_bytes = settings.database_path.read_bytes()
    assert b"fresh-access-token" in database_bytes
    assert b"fresh-refresh-token" in database_bytes
    assert settings.database_path.stat().st_mode & 0o777 == 0o600


def test_oauth_callback_rejects_a_missing_or_mismatched_state(client: TestClient) -> None:
    begin_authorization(client)

    response = client.get(
        "/auth/strava/callback",
        params={
            "code": "authorization-code",
            "state": "wrong-state",
            "scope": "activity:read_all,activity:write",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid OAuth state"


def test_authorization_canonicalizes_the_origin_before_setting_the_state_cookie(
    settings: Settings,
    fake_strava: FakeStravaClient,
) -> None:
    canonical_settings = Settings(
        strava_client_id=settings.strava_client_id,
        strava_client_secret=settings.strava_client_secret,
        database_path=settings.database_path,
        base_url="http://127.0.0.1:8000",
    )
    app = create_app(settings=canonical_settings, strava_client=fake_strava)
    client = TestClient(app, base_url="http://localhost:8000")

    response = client.get("/auth/strava", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://127.0.0.1:8000/auth/strava"
    assert client.cookies.get("commuter_oauth_state") is None


def test_oauth_callback_requires_all_scopes(client: TestClient, fake_strava: FakeStravaClient) -> None:
    state = begin_authorization(client)

    response = client.get(
        "/auth/strava/callback",
        params={
            "code": "authorization-code",
            "state": state,
            "scope": "activity:read_all",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Required Strava permissions were not granted"
    assert fake_strava.exchanged_code is None


def test_status_and_disconnect_revoke_then_delete_local_credentials(
    client: TestClient,
    fake_strava: FakeStravaClient,
) -> None:
    state = begin_authorization(client)
    callback = client.get(
        "/auth/strava/callback",
        params={
            "code": "authorization-code",
            "state": state,
            "scope": "activity:read_all,activity:write",
        },
        follow_redirects=False,
    )
    assert callback.status_code == 303

    status = client.get("/auth/strava/status")
    assert status.status_code == 200
    assert status.json() == {
        "athlete_id": 123,
        "connected": True,
        "scopes": ["activity:read_all", "activity:write"],
    }

    csrf_token = client.cookies.get("commuter_csrf")
    response = client.post("/auth/strava/disconnect", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 204
    assert fake_strava.revoked_token == "fresh-refresh-token"
    assert client.get("/auth/strava/status").status_code == 401


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_and_rotated(
    settings: Settings,
    fake_strava: FakeStravaClient,
) -> None:
    from commuter.auth import TokenManager

    store = CredentialStore(settings.database_path)
    store.save_account(
        athlete=Athlete(id=123, username="commuter"),
        scopes={"activity:read_all", "activity:write"},
        tokens=TokenSet(
            access_token="expired-access-token",
            refresh_token="refresh-token",
            expires_at=1,
            athlete=Athlete(id=123, username="commuter"),
        ),
    )
    token_manager = TokenManager(store=store, strava_client=fake_strava)

    access_token = await token_manager.get_access_token(123)

    assert access_token == "refreshed-access-token"
    assert fake_strava.refreshed_token == "refresh-token"
    account = store.get_account(123)
    assert account is not None
    assert account.refresh_token == "rotated-refresh-token"
