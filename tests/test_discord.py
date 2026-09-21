from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from commuter.config import Settings
from commuter.discord import DiscordNotifier


@dataclass
class FakeHTTPClient:
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = field(default_factory=list)

    async def __aenter__(self) -> "FakeHTTPClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> "FakeResponse":
        self.calls.append((url, headers, json))
        return FakeResponse()


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


@pytest.mark.asyncio
async def test_discord_notifier_posts_an_activity_update_without_parsing_mentions(monkeypatch, tmp_path: Path) -> None:
    from commuter import discord

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        base_url="http://127.0.0.1:8000",
        discord_bot_token="discord-token",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
    )
    http_client = FakeHTTPClient()
    monkeypatch.setattr(discord.httpx, "AsyncClient", lambda timeout: http_client)

    await DiscordNotifier(settings).activity_updated(
        activity_id=123,
        estimated_savings="$0.87",
        cumulative_savings="$1.04",
        co2_avoided="1.78 kg",
        cumulative_co2_avoided="2.13 kg",
    )

    assert http_client.calls == [
        (
            "https://discord.com/api/v10/channels/234567890123456789/messages",
            {"Authorization": "Bot discord-token"},
            {
                "content": (
                    "🚲 Commuter updated <https://www.strava.com/activities/123>\n"
                    "Estimated fuel savings: $0.87\n"
                    "Cumulative fuel savings: $1.04\n"
                    "CO₂ avoided: 1.78 kg\n"
                    "Cumulative CO₂ avoided: 2.13 kg"
                ),
                "allowed_mentions": {"parse": []},
            },
        )
    ]
