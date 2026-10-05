"""The narrow HTTP client needed for Strava OAuth token operations."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx

from commuter.config import Settings
from commuter.models import Athlete, TokenSet

MAX_RATE_LIMIT_RETRIES = 1
MAX_INLINE_RATE_LIMIT_BACKOFF_SECONDS = 60
RATE_LIMIT_WINDOW_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


class StravaAPIError(RuntimeError):
    """Raised when Strava cannot complete an OAuth operation."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StravaRateLimitError(StravaAPIError):
    """Raised when the next retry is too far away for this polling run."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "Strava rate-limited the activity request (HTTP 429). "
            f"Retry after {retry_after_seconds} seconds."
        )
        self.retry_after_seconds = retry_after_seconds


class StravaClient:
    """Call the limited OAuth and activity endpoints needed by Commuter."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = None
        self._retry_at = 0.0
        self.deadline: float | None = None

    async def __aenter__(self):
        self._session = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *args):
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    @asynccontextmanager
    async def _activity_session(self):
        if self._retry_at > time.time():
            raise StravaRateLimitError(math.ceil(self._retry_at - time.time()))
        if self.deadline is not None and time.monotonic() + 12 >= self.deadline:
            raise StravaRateLimitError(1)
        if self._session is not None:
            yield self._session
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                yield client

    async def exchange_authorization_code(self, code: str) -> TokenSet:
        """Exchange a one-time authorization code for an athlete token set."""

        payload = await self._post_token(
            {
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            }
        )
        token_set = _token_set_from_payload(payload)
        if token_set.athlete is None:
            raise StravaAPIError("Strava did not return an athlete for the authorization code")
        return token_set

    async def refresh_access_token(self, refresh_token: str) -> TokenSet:
        """Refresh and rotate a Strava access token when needed."""

        payload = await self._post_token(
            {
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        return _token_set_from_payload(payload)

    async def revoke(self, refresh_token: str) -> None:
        """Revoke Strava authorization before locally deleting credentials."""

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._settings.strava_revoke_url,
                    auth=(self._settings.strava_client_id, self._settings.strava_client_secret),
                    data={"token": refresh_token, "token_type_hint": "refresh_token"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StravaAPIError("Strava could not revoke the connection") from exc

    async def list_athlete_activities(
        self,
        access_token: str,
        *,
        after: int,
        per_page: int = 100,
        page: int = 1,
        before: int | None = None,
    ) -> list[dict[str, object]]:
        """List activities that occurred after a configured commuter rule began."""

        params = {"after": after, "per_page": per_page}
        if page != 1:
            params["page"] = page
        if before is not None:
            params["before"] = before
        payload = await self._get_activity_json(
            "/athlete/activities",
            access_token,
            params=params,
        )
        if not isinstance(payload, list) or not all(
            isinstance(activity, dict) for activity in payload
        ):
            raise StravaAPIError("Strava returned an invalid activity list")
        return payload

    async def get_activity_streams(
        self, access_token: str, activity_id: int
    ) -> dict[str, object]:
        try:
            payload = await self._get_activity_json(
                f"/activities/{activity_id}/streams",
                access_token,
                params={
                    "keys": "time,heartrate,moving,distance",
                    "key_by_type": "true",
                },
            )
        except StravaAPIError as exc:
            if exc.status_code == 404:
                return {}  # Detail exists, but this workout has no recorded streams.
            raise
        if not isinstance(payload, dict):
            raise StravaAPIError("Strava returned invalid activity streams")
        return payload

    async def get_activity_zones(self, access_token: str, activity_id: int) -> dict:
        """Read platform-reported zones without inventing personal thresholds."""
        try:
            payload = await self._get_activity_json(
                f"/activities/{activity_id}/zones", access_token, params={}
            )
        except StravaAPIError as exc:
            if exc.status_code in {402, 403, 404}:
                return {
                    "availability": {
                        402: "subscription_required",
                        403: "zones_not_authorized",
                        404: "zones_unavailable",
                    }[exc.status_code],
                    "zones": [],
                }
            raise
        if not isinstance(payload, list) or not all(
            isinstance(z, dict) for z in payload
        ):
            raise StravaAPIError("Strava returned invalid activity zones")
        return {"availability": "strava_reported", "zones": payload}

    async def get_activity(self, access_token: str, activity_id: int) -> dict[str, object]:
        """Fetch the current description and endpoint coordinates for an activity."""

        payload = await self._get_activity_json(f"/activities/{activity_id}", access_token, params={})
        if not isinstance(payload, dict):
            raise StravaAPIError("Strava returned an invalid activity")
        return payload

    async def update_activity(
        self, access_token: str, activity_id: int, update: dict[str, object]
    ) -> None:
        """Set commute metadata and the managed description block on an activity."""

        try:
            async with self._activity_session() as client:
                for retry_attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
                    response = await client.put(
                        f"{self._settings.strava_api_base_url}/activities/{activity_id}",
                        headers=self._activity_headers(access_token),
                        json=update,
                    )
                    self._observe_quota(response)
                    if response.status_code == 429:
                        await self._backoff_after_rate_limit(response, retry_attempt)
                        continue
                    response.raise_for_status()
                    return
        except httpx.HTTPError as exc:
            raise StravaAPIError("Strava could not update the activity") from exc

    async def _get_activity_json(
        self,
        path: str,
        access_token: str,
        *,
        params: dict[str, object],
    ) -> object:
        try:
            async with self._activity_session() as client:
                for retry_attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
                    response = await client.get(
                        f"{self._settings.strava_api_base_url}{path}",
                        headers=self._activity_headers(access_token),
                        params=params,
                    )
                    self._observe_quota(response)
                    if response.status_code == 429:
                        await self._backoff_after_rate_limit(response, retry_attempt)
                        continue
                    response.raise_for_status()
                    return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StravaAPIError(
                _activity_request_error(exc),
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
            ) from exc

        raise AssertionError("Rate-limit retry loop ended without returning or raising")

    async def _backoff_after_rate_limit(
        self, response: httpx.Response, retry_attempt: int
    ) -> None:
        """Sleep for a short server-directed retry delay, or defer the poll."""

        retry_after_seconds = _rate_limit_backoff_seconds(response)
        if self._retry_at > time.time():
            retry_after_seconds = max(
                retry_after_seconds, math.ceil(self._retry_at - time.time())
            )
        if (
            self.deadline is not None
            and time.monotonic() + retry_after_seconds + 12 >= self.deadline
        ):
            raise StravaRateLimitError(retry_after_seconds)
        if (
            retry_attempt >= MAX_RATE_LIMIT_RETRIES
            or retry_after_seconds > MAX_INLINE_RATE_LIMIT_BACKOFF_SECONDS
        ):
            logger.info(
                "Strava rate-limited; retry needs %s seconds and is deferred to the next scheduled poll",
                retry_after_seconds,
            )
            raise StravaRateLimitError(retry_after_seconds)
        logger.info(
            "Strava rate-limited; backing off for %s seconds before retry %s of %s",
            retry_after_seconds,
            retry_attempt + 1,
            MAX_RATE_LIMIT_RETRIES,
        )
        await asyncio.sleep(retry_after_seconds)

    def _observe_quota(self, response) -> None:
        headers = getattr(response, "headers", {})
        now = int(time.time())
        delays = []
        for prefix in ("X-RateLimit", "X-ReadRateLimit"):
            try:
                limits = [int(v) for v in headers[f"{prefix}-Limit"].split(",")]
                usage = [int(v) for v in headers[f"{prefix}-Usage"].split(",")]
                if len(limits) != 2 or len(usage) != 2:
                    continue
                if usage[0] >= limits[0]:
                    delays.append(
                        RATE_LIMIT_WINDOW_SECONDS - now % RATE_LIMIT_WINDOW_SECONDS + 1
                    )
                if usage[1] >= limits[1]:
                    delays.append(86400 - now % 86400 + 1)
            except (KeyError, TypeError, ValueError):
                continue
        if delays:
            self._retry_at = now + max(delays)

    @staticmethod
    def _activity_headers(access_token: str) -> dict[str, str]:
        """Return the bearer authorization header without logging the token."""

        return {"Authorization": f"Bearer {access_token}"}

    async def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._settings.strava_token_url, data=data)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StravaAPIError("Strava could not issue credentials") from exc

        if not isinstance(payload, dict):
            raise StravaAPIError("Strava returned an invalid credential response")
        return payload


def _token_set_from_payload(payload: dict[str, Any]) -> TokenSet:
    try:
        access_token = payload["access_token"]
        refresh_token = payload["refresh_token"]
        expires_at = int(payload["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StravaAPIError("Strava returned incomplete credentials") from exc

    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise StravaAPIError("Strava returned invalid credentials")

    athlete_payload = payload.get("athlete")
    athlete: Athlete | None = None
    if isinstance(athlete_payload, dict):
        try:
            athlete = Athlete(
                id=int(athlete_payload["id"]),
                username=_optional_string(athlete_payload.get("username")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StravaAPIError("Strava returned an invalid athlete") from exc

    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        athlete=athlete,
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _activity_request_error(error: httpx.HTTPError | ValueError) -> str:
    """Describe an activity request failure without revealing request data."""

    if isinstance(error, httpx.ConnectError):
        return "Could not connect to the Strava API. Check Internet and DNS connectivity, then try again."
    if isinstance(error, httpx.TimeoutException):
        return "The Strava API request timed out. Try again shortly."
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 401:
            return "Strava rejected the activity request (HTTP 401). Reconnect Commuter and try again."
        if error.response.status_code == 403:
            return "Strava rejected the activity request (HTTP 403). Reconnect Commuter and approve its requested scopes."
        if error.response.status_code == 429:
            return "Strava rate-limited the activity request (HTTP 429). Retryable rate-limit handling failed."
        return f"Strava rejected the activity request (HTTP {error.response.status_code})."
    return "Strava could not retrieve activities"


def _rate_limit_backoff_seconds(response: httpx.Response) -> int:
    """Use Strava's delay header or wait for its next 15-minute rate window."""

    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(1, int(retry_after))
        except ValueError:
            pass
    now = int(time.time())
    next_window = (now // RATE_LIMIT_WINDOW_SECONDS + 1) * RATE_LIMIT_WINDOW_SECONDS
    return max(1, next_window - now + 1)
