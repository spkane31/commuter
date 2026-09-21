from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from commuter.config import Settings
from commuter.models import Athlete, TokenSet
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError


@dataclass
class FakeStravaClient:
    revoked_tokens: list[str] = field(default_factory=list)
    revoke_error: StravaAPIError | None = None

    async def revoke(self, refresh_token: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked_tokens.append(refresh_token)


def test_wipe_command_revokes_and_removes_all_local_state(monkeypatch, tmp_path: Path) -> None:
    import commuter.main as cli

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        base_url="http://127.0.0.1:8000",
    )
    store = CredentialStore(settings.database_path)
    store.save_account(
        athlete=Athlete(id=123, username="commuter"),
        scopes={"activity:read_all", "activity:write"},
        tokens=TokenSet(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            athlete=Athlete(id=123, username="commuter"),
        ),
    )
    store.close()
    database_wal = settings.database_path.with_name("commuter.db-wal")
    database_shm = settings.database_path.with_name("commuter.db-shm")
    database_wal.write_text("temporary sqlite state", encoding="utf-8")
    database_shm.write_text("temporary sqlite state", encoding="utf-8")

    fake_strava = FakeStravaClient()

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> Settings:
            return settings

    monkeypatch.setattr(cli, "Settings", FakeSettings, raising=False)
    monkeypatch.setattr(cli, "StravaClient", lambda _: fake_strava, raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["commuter", "wipe"])

    cli.main()

    assert fake_strava.revoked_tokens == ["refresh-token"]
    assert not settings.database_path.exists()
    assert not database_wal.exists()
    assert not database_shm.exists()


def test_force_local_wipe_removes_state_when_revocation_fails(monkeypatch, tmp_path: Path) -> None:
    import commuter.main as cli

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        base_url="http://127.0.0.1:8000",
    )
    store = CredentialStore(settings.database_path)
    store.save_account(
        athlete=Athlete(id=123, username="commuter"),
        scopes={"activity:read_all", "activity:write"},
        tokens=TokenSet(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            athlete=Athlete(id=123, username="commuter"),
        ),
    )
    store.close()

    fake_strava = FakeStravaClient(revoke_error=StravaAPIError("Strava unavailable"))

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> Settings:
            return settings

    monkeypatch.setattr(cli, "Settings", FakeSettings, raising=False)
    monkeypatch.setattr(cli, "StravaClient", lambda _: fake_strava, raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["commuter", "wipe", "--force-local"])

    cli.main()

    assert fake_strava.revoked_tokens == []
    assert not settings.database_path.exists()
