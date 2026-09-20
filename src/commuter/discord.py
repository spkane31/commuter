"""Outbound Discord notifications for successfully updated commutes."""

from __future__ import annotations

import httpx

from commuter.config import ConfigurationError, Settings

DISCORD_API_BASE_URL = "https://discord.com/api/v10"


class DiscordAPIError(RuntimeError):
    """Raised when Discord cannot accept a Commuter activity notification."""


class DiscordNotifier:
    """Send activity-update notifications through a configured Discord bot."""

    def __init__(self, settings: Settings) -> None:
        _validate_discord_configuration(settings)
        self._bot_token = settings.discord_bot_token
        self._channel_id = settings.discord_channel_id

    async def activity_updated(
        self,
        *,
        activity_id: int,
        estimated_savings: str,
        cumulative_savings: str,
        co2_avoided: str,
        cumulative_co2_avoided: str,
    ) -> None:
        """Post the savings summary after Strava accepts an activity update."""

        payload = {
            "content": (
                f"🚲 Commuter updated <https://www.strava.com/activities/{activity_id}>\n"
                f"Estimated fuel savings: {estimated_savings}\n"
                f"Cumulative fuel savings: {cumulative_savings}\n"
                f"CO₂ avoided: {co2_avoided}\n"
                f"Cumulative CO₂ avoided: {cumulative_co2_avoided}"
            ),
            "allowed_mentions": {"parse": []},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{DISCORD_API_BASE_URL}/channels/{self._channel_id}/messages",
                    headers={"Authorization": f"Bot {self._bot_token}"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DiscordAPIError("Discord could not send the activity update notification") from exc


def _validate_discord_configuration(settings: Settings) -> None:
    """Require the owner-provided Discord destination before a live sync."""

    missing = [
        name
        for name, value in (
            ("DISCORD_BOT_TOKEN", settings.discord_bot_token),
            ("DISCORD_GUILD_ID", settings.discord_guild_id),
            ("DISCORD_CHANNEL_ID", settings.discord_channel_id),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required for sync")
