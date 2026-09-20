"""Local configuration and secret-key management."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the local web service."""

    strava_client_id: str
    strava_client_secret: str
    database_path: Path
    encryption_key_path: Path
    base_url: str
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_channel_id: str = ""
    strava_authorize_url: str = "https://www.strava.com/oauth/authorize"
    strava_token_url: str = "https://www.strava.com/oauth/token"
    strava_revoke_url: str = "https://www.strava.com/oauth/revoke"
    strava_api_base_url: str = "https://www.strava.com/api/v3"

    def __post_init__(self) -> None:
        if not self.strava_client_id:
            raise ConfigurationError("STRAVA_CLIENT_ID is required")
        if not self.strava_client_secret:
            raise ConfigurationError("STRAVA_CLIENT_SECRET is required")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigurationError("COMMUTER_BASE_URL must be an HTTP(S) URL")

    @property
    def callback_url(self) -> str:
        """Return the OAuth callback URL registered with Strava."""

        return f"{self.base_url.rstrip('/')}/auth/strava/callback"

    @property
    def secure_cookies(self) -> bool:
        """Only mark cookies secure when the configured local URL uses HTTPS."""

        return self.base_url.startswith("https://")

    @classmethod
    def from_environment(cls, env_file: Path | None = None) -> "Settings":
        """Build settings from the environment and an optional local .env file."""

        _load_dotenv(env_file or Path(".env"))
        return cls(
            strava_client_id=_required_environment_value("STRAVA_CLIENT_ID"),
            strava_client_secret=_required_environment_value("STRAVA_CLIENT_SECRET"),
            database_path=Path(os.environ.get("COMMUTER_DATABASE_PATH", "commuter.db")),
            encryption_key_path=Path(os.environ.get("COMMUTER_ENCRYPTION_KEY_PATH", ".commuter.key")),
            base_url=os.environ.get("COMMUTER_BASE_URL", "http://127.0.0.1:8000"),
            discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
            discord_guild_id=os.environ.get("DISCORD_GUILD_ID", "").strip(),
            discord_channel_id=os.environ.get("DISCORD_CHANNEL_ID", "").strip(),
        )

    def load_or_create_encryption_key(self) -> bytes:
        """Load the local Fernet key or atomically create one with mode 0600."""

        self.encryption_key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        try:
            key = self.encryption_key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    self.encryption_key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.encryption_key_path.read_bytes().strip()
            else:
                with os.fdopen(descriptor, "wb") as key_file:
                    key_file.write(key)

        try:
            Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"Invalid Fernet key at {self.encryption_key_path}; replace it only if the credential database is empty"
            ) from exc
        return key


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _load_dotenv(path: Path) -> None:
    """Load uncomplicated KEY=value entries without overwriting exported values."""

    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
