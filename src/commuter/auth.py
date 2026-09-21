"""OAuth session and access-token lifecycle helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from commuter.models import Account
from commuter.store import CredentialStore
from commuter.strava import StravaClient


class NotConnectedError(LookupError):
    """Raised when a requested local athlete has no stored connection."""


class SessionCodec:
    """Sign minimal browser sessions using the Strava client secret."""

    def __init__(self, client_secret: str) -> None:
        self._key = hashlib.sha256(b"commuter-session-v1:" + client_secret.encode("utf-8")).digest()

    def encode(self, athlete_id: int) -> str:
        """Sign a local athlete ID for a browser session cookie."""

        value = str(athlete_id).encode("ascii")
        signature = hmac.new(self._key, value, hashlib.sha256).digest()
        return f"{value.decode('ascii')}.{_urlsafe_encode(signature)}"

    def decode(self, value: str | None) -> int | None:
        """Validate a session cookie and return its athlete ID."""

        if not value or "." not in value:
            return None
        identifier, encoded_signature = value.rsplit(".", 1)
        if not identifier.isdigit():
            return None
        try:
            signature = _urlsafe_decode(encoded_signature)
        except ValueError:
            return None
        expected = hmac.new(self._key, identifier.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        return int(identifier)


class TokenManager:
    """Return a valid access token and persist Strava token rotation."""

    def __init__(self, store: CredentialStore, strava_client: StravaClient) -> None:
        self._store = store
        self._strava_client = strava_client

    async def get_access_token(self, athlete_id: int) -> str:
        """Return a non-expiring-soon token, refreshing it when necessary."""

        account = self._require_account(athlete_id)
        if account.expires_at > int(time.time()) + 60:
            return account.access_token

        refreshed = await self._strava_client.refresh_access_token(account.refresh_token)
        self._store.save_account(
            athlete=refreshed.athlete or account.athlete,
            scopes=account.scopes,
            tokens=refreshed,
        )
        return refreshed.access_token

    def _require_account(self, athlete_id: int) -> Account:
        account = self._store.get_account(athlete_id)
        if account is None:
            raise NotConnectedError(f"No local Strava connection exists for athlete {athlete_id}")
        return account


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid base64 signature") from exc
