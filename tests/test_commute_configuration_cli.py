from __future__ import annotations

import sys
from pathlib import Path

import pytest

from commuter.config import Settings
from commuter.models import Athlete, TokenSet
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError


def test_configure_commute_command_saves_the_location_rule(monkeypatch, tmp_path: Path, capsys) -> None:
    import commuter.main as cli

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        base_url="http://127.0.0.1:8000",
        discord_bot_token="discord-token",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
    )
    store = CredentialStore(settings.database_path)
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
            "--location",
            "home,39.781003858657165,-105.02303718996976",
            "--location",
            "work,39.74341292772691,-104.9886192024491",
            "--location",
            "gym,39.75123,-105.00110",
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
    store = CredentialStore(settings.database_path)
    configuration = store.get_commute_configuration(123)
    assert configuration is not None
    locations_by_name = {location.name: location.coordinate for location in configuration.locations}
    assert locations_by_name["home"].latitude == 39.781003858657165
    assert locations_by_name["work"].longitude == -104.9886192024491
    assert locations_by_name["gym"].latitude == 39.75123
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
