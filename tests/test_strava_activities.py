from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from commuter.config import Settings
from commuter.strava import StravaAPIError, StravaClient


def test_default_strava_api_base_url_uses_the_official_v3_endpoint(tmp_path: Path) -> None:
    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
    )

    assert settings.strava_api_base_url == "https://www.strava.com/api/v3"


@dataclass
class FakeHTTPClient:
    calls: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    async def __aenter__(self) -> "FakeHTTPClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, object]):
        self.calls.append(("GET", url, {"headers": headers, "params": params}))
        if url.endswith("/athlete/activities"):
            return FakeResponse([{"id": 123}])
        return FakeResponse({"id": 123, "sport_type": "Ride"})

    async def put(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
        self.calls.append(("PUT", url, {"headers": headers, "json": json}))
        return FakeResponse({"id": 123})


@dataclass
class FakeResponse:
    payload: object
    status_code: int = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


@dataclass
class FailingHTTPClient:
    async def __aenter__(self) -> "FailingHTTPClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, *args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("name lookup failed")


@dataclass
class ForbiddenHTTPClient:
    async def __aenter__(self) -> "ForbiddenHTTPClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("GET", url))


@dataclass
class RateLimitedHTTPClient:
    calls: int = 0

    async def __aenter__(self) -> "RateLimitedHTTPClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        request = httpx.Request("GET", url)
        if self.calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, request=request)
        return httpx.Response(200, json=[{"id": 123}], request=request)


@pytest.mark.asyncio
async def test_activity_client_lists_fetches_and_updates_the_authenticated_athletes_activity(monkeypatch, tmp_path: Path) -> None:
    from commuter import strava

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
        strava_api_base_url="https://strava.test/api/v3",
    )
    http_client = FakeHTTPClient()
    monkeypatch.setattr(strava.httpx, "AsyncClient", lambda timeout: http_client)
    client = StravaClient(settings)

    activities = await client.list_athlete_activities("access-token", after=1234, per_page=100)
    activity = await client.get_activity("access-token", 123)
    await client.update_activity("access-token", 123, {"commute": True, "description": "Commuter details"})

    assert activities == [{"id": 123}]
    assert activity == {"id": 123, "sport_type": "Ride"}
    assert http_client.calls == [
        (
            "GET",
            "https://strava.test/api/v3/athlete/activities",
            {"headers": {"Authorization": "Bearer access-token"}, "params": {"after": 1234, "per_page": 100}},
        ),
        (
            "GET",
            "https://strava.test/api/v3/activities/123",
            {"headers": {"Authorization": "Bearer access-token"}, "params": {}},
        ),
        (
            "PUT",
            "https://strava.test/api/v3/activities/123",
            {
                "headers": {"Authorization": "Bearer access-token"},
                "json": {"commute": True, "description": "Commuter details"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_activity_client_reports_a_connectivity_error_without_request_details(monkeypatch, tmp_path: Path) -> None:
    from commuter import strava

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
    )
    monkeypatch.setattr(strava.httpx, "AsyncClient", lambda timeout: FailingHTTPClient())

    with pytest.raises(StravaAPIError, match="Could not connect to the Strava API"):
        await StravaClient(settings).list_athlete_activities("access-token", after=1234)


@pytest.mark.asyncio
async def test_activity_client_recommends_reconnecting_after_a_forbidden_response(monkeypatch, tmp_path: Path) -> None:
    from commuter import strava

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
    )
    monkeypatch.setattr(strava.httpx, "AsyncClient", lambda timeout: ForbiddenHTTPClient())

    with pytest.raises(StravaAPIError, match="Reconnect Commuter"):
        await StravaClient(settings).list_athlete_activities("access-token", after=1234)


@pytest.mark.asyncio
async def test_activity_client_retries_rate_limited_requests_after_retry_after_delay(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from commuter import strava

    settings = Settings(
        strava_client_id="12345",
        strava_client_secret="test-client-secret",
        database_path=tmp_path / "commuter.db",
        encryption_key_path=tmp_path / "commuter.key",
        base_url="http://127.0.0.1:8000",
    )
    http_client = RateLimitedHTTPClient()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(strava.httpx, "AsyncClient", lambda timeout: http_client)
    monkeypatch.setattr(strava.asyncio, "sleep", fake_sleep)
    caplog.set_level(logging.INFO, logger="commuter.strava")

    activities = await StravaClient(settings).list_athlete_activities("access-token", after=1234)

    assert activities == [{"id": 123}]
    assert http_client.calls == 2
    assert delays == [2]
    assert "rate-limited; backing off for 2 seconds before retry 1 of 1" in caplog.text
