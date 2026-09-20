from __future__ import annotations

import sys
from pathlib import Path

import pytest

from commuter.config import Settings
from commuter.models import Athlete, TokenSet
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError


def test_configure_commute_command_saves_the_home_work_rule(monkeypatch, tmp_path: Path, capsys) -> None:
    import commuter.main as cli

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
        discord_bot_token="discord-token",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
    )
    store = CredentialStore(settings.database_path, settings.load_or_create_encryption_key())
    athlete = Athlete(id=123, username="commuter")
    store.save_account(
        athlete=athlete,
        scopes={"activity:read_all", "activity:write"},
        tokens=TokenSet(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            athlete=athlete,
        ),
    )
    store.close()

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> Settings:
            return settings

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "commuter",
            "configure-commute",
            "--home",
            "39.781003858657165,-105.02303718996976",
            "--work",
            "39.74341292772691,-104.9886192024491",
            "--radius-m",
            "150",
            "--combined-mpg",
            "25",
            "--gas-price",
            "4.34",
            "--vehicle",
            "2016 Subaru Forester",
        ],
    )

    cli.main()

    output = capsys.readouterr().out
    assert output == "Commute configuration saved for athlete 123.\n"
    store = CredentialStore(settings.database_path, settings.load_or_create_encryption_key())
    configuration = store.get_commute_configuration(123)
    assert configuration is not None
    assert configuration.home.latitude == 39.781003858657165
    assert configuration.work.longitude == -104.9886192024491
    assert configuration.radius_m == 150
    assert configuration.combined_mpg == 25.0
    assert configuration.gas_price_cents == 434
    assert configuration.vehicle_name == "2016 Subaru Forester"


def test_sync_command_reports_strava_errors_without_a_traceback(monkeypatch, tmp_path: Path, capsys) -> None:
    import commuter.main as cli

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
        discord_bot_token="discord-token",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
    )

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> Settings:
            return settings

    async def unavailable_sync(**kwargs: object) -> object:
        raise StravaAPIError("Strava could not retrieve activities")

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "synchronize_commutes", unavailable_sync)
    monkeypatch.setattr(sys, "argv", ["commuter", "sync", "--dry-run"])

    with pytest.raises(SystemExit) as exit_result:
        cli.main()

    assert exit_result.value.code == 2
    assert "error: Strava could not retrieve activities" in capsys.readouterr().err
