from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from commuter.commute import CommuteConfiguration, Coordinate, synchronize_commutes
from commuter.models import ActivityProcessing, Athlete, Location, TokenSet
from commuter.store import CredentialStore


HOME = Coordinate(latitude=39.781003858657165, longitude=-105.02303718996976)
WORK = Coordinate(latitude=39.74341292772691, longitude=-104.9886192024491)
GYM = Coordinate(latitude=39.75123, longitude=-105.00110)


@dataclass
class FakeTokenManager:
    requested_athlete_ids: list[int] = field(default_factory=list)

    async def get_access_token(self, athlete_id: int) -> str:
        self.requested_athlete_ids.append(athlete_id)
        return "access-token"


@dataclass
class FakeActivityClient:
    details: dict[int, dict[str, object]]
    updates: list[tuple[int, dict[str, object]]] = field(default_factory=list)
    summaries: list[dict[str, object]] = field(
        default_factory=lambda: [
            {"id": 200, "start_date": "2026-09-17T18:00:00Z"},
            {"id": 100, "start_date": "2026-09-17T08:00:00Z"},
            {"id": 300, "start_date": "2026-09-17T12:00:00Z"},
        ]
    )

    async def list_athlete_activities(
        self,
        access_token: str,
        *,
        after: int,
        per_page: int = 100,
    ) -> list[dict[str, object]]:
        assert access_token == "access-token"
        assert after > 0
        assert per_page == 100
        return self.summaries

    async def get_activity(self, access_token: str, activity_id: int) -> dict[str, object]:
        assert access_token == "access-token"
        return self.details[activity_id]

    async def update_activity(self, access_token: str, activity_id: int, update: dict[str, object]) -> None:
        assert access_token == "access-token"
        self.updates.append((activity_id, update))


@dataclass
class FakeNotifier:
    notifications: list[tuple[int, str, str, str, str]] = field(default_factory=list)

    async def activity_updated(
        self,
        *,
        activity_id: int,
        estimated_savings: str,
        cumulative_savings: str,
        co2_avoided: str = "",
        cumulative_co2_avoided: str = "",
    ) -> None:
        self.notifications.append(
            (activity_id, estimated_savings, cumulative_savings, co2_avoided, cumulative_co2_avoided)
        )


@dataclass
class InspectingNotifier:
    store: CredentialStore
    observed_processing: list[ActivityProcessing | None] = field(default_factory=list)

    async def activity_updated(
        self,
        *,
        activity_id: int,
        estimated_savings: str,
        cumulative_savings: str,
        co2_avoided: str = "",
        cumulative_co2_avoided: str = "",
    ) -> None:
        self.observed_processing.append(self.store.get_activity_processing(123, activity_id))


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    store = CredentialStore(tmp_path / "commuter.db")
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
    store.save_commute_configuration(
        CommuteConfiguration(
            athlete_id=123,
            locations=(Location(name="home", coordinate=HOME), Location(name="work", coordinate=WORK)),
            radius_m=150,
            combined_mpg=25.0,
            gas_price_cents=434,
            vehicle_name="2016 Subaru Forester",
            currency="USD",
        )
    )
    return store


@pytest.fixture
def store_with_gym(store: CredentialStore) -> CredentialStore:
    store.save_commute_configuration(
        CommuteConfiguration(
            athlete_id=123,
            locations=(
                Location(name="home", coordinate=HOME),
                Location(name="work", coordinate=WORK),
                Location(name="gym", coordinate=GYM),
            ),
            radius_m=150,
            combined_mpg=25.0,
            gas_price_cents=434,
            vehicle_name="2016 Subaru Forester",
            currency="USD",
        )
    )
    return store


@pytest.mark.asyncio
async def test_sync_uses_each_ride_distance_for_bidirectional_commute_savings(store: CredentialStore) -> None:
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "start_latlng": [HOME.latitude, HOME.longitude],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "distance": 8_046.72,
                "description": "Morning ride",
            },
            200: {
                "id": 200,
                "sport_type": "Ride",
                "start_latlng": [WORK.latitude, WORK.longitude],
                "end_latlng": [HOME.latitude, HOME.longitude],
                "distance": 1_609.344,
                "description": None,
            },
            300: {
                "id": 300,
                "sport_type": "Ride",
                "start_latlng": [39.70, -105.10],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "description": "Errand",
            },
        }
    )
    tokens = FakeTokenManager()
    notifier = FakeNotifier()

    first_result = await synchronize_commutes(
        store=store,
        token_manager=tokens,
        strava_client=client,
        notifier=notifier,
    )

    assert first_result.updated_activity_ids == [100, 200]
    assert first_result.non_matching_activity_ids == [300]
    assert client.updates == [
        (
            100,
            {
                "commute": True,
                "hide_from_home": True,
                "description": (
                    "Morning ride\n\n"
                    "--- Commuter ---\n"
                    "Fuel avoided: 0.20 gal\n"
                    "CO₂ avoided: 1.78 kg\n"
                    "Estimated fuel savings: $0.87\n"
                    "Cumulative fuel savings: $0.87\n"
                    "Cumulative CO₂ avoided: 1.78 kg\n"
                    "--- /Commuter ---"
                ),
            },
        ),
        (
            200,
            {
                "commute": True,
                "hide_from_home": True,
                "description": (
                    "--- Commuter ---\n"
                    "Fuel avoided: 0.04 gal\n"
                    "CO₂ avoided: 0.36 kg\n"
                    "Estimated fuel savings: $0.17\n"
                    "Cumulative fuel savings: $1.04\n"
                    "Cumulative CO₂ avoided: 2.13 kg\n"
                    "--- /Commuter ---"
                ),
            },
        ),
    ]
    assert notifier.notifications == [
        (100, "$0.87", "$0.87", "1.78 kg", "1.78 kg"),
        (200, "$0.17", "$1.04", "0.36 kg", "2.13 kg"),
    ]
    assert store.get_commute_configuration(123).cumulative_savings_cents == 104
    assert store.get_commute_configuration(123).cumulative_co2_avoided_grams == 2_132

    second_result = await synchronize_commutes(
        store=store,
        token_manager=tokens,
        strava_client=client,
        notifier=notifier,
    )

    assert second_result.updated_activity_ids == []
    assert second_result.non_matching_activity_ids == []
    assert len(client.updates) == 2
    assert len(notifier.notifications) == 2
    assert store.get_commute_configuration(123).cumulative_savings_cents == 104


@pytest.mark.asyncio
async def test_dry_run_backfill_reports_matching_rides_without_updating_strava_or_sqlite(store: CredentialStore) -> None:
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "start_latlng": [HOME.latitude, HOME.longitude],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "distance": 7_242.048,
                "description": "Morning ride",
            },
            200: {
                "id": 200,
                "sport_type": "Ride",
                "start_latlng": [WORK.latitude, WORK.longitude],
                "end_latlng": [HOME.latitude, HOME.longitude],
                "distance": 7_242.048,
                "description": None,
            },
            300: {
                "id": 300,
                "sport_type": "Ride",
                "start_latlng": [39.70, -105.10],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "description": "Errand",
            },
        }
    )

    result = await synchronize_commutes(
        store=store,
        token_manager=FakeTokenManager(),
        strava_client=client,
        notifier=FakeNotifier(),
        after=1,
        dry_run=True,
    )

    assert result.would_update_activity_ids == [100, 200]
    assert result.updated_activity_ids == []
    assert result.non_matching_activity_ids == [300]
    assert client.updates == []
    assert store.get_commute_configuration(123).cumulative_savings_cents == 0
    assert store.get_activity_processing(123, 100) is None


@pytest.mark.asyncio
async def test_sync_sends_the_discord_notification_before_writing_activity_processing_state(store: CredentialStore) -> None:
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "start_latlng": [HOME.latitude, HOME.longitude],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "distance": 7_242.048,
                "description": "Morning ride",
            },
        },
        summaries=[{"id": 100, "start_date": "2026-09-17T08:00:00Z"}],
    )
    notifier = InspectingNotifier(store)

    result = await synchronize_commutes(
        store=store,
        token_manager=FakeTokenManager(),
        strava_client=client,
        notifier=notifier,
    )

    assert result.updated_activity_ids == [100]
    assert notifier.observed_processing == [None]
    assert store.get_activity_processing(123, 100).status == "completed"


@pytest.mark.asyncio
async def test_recheck_logs_match_reasons_and_processes_a_manually_tagged_commute(
    store: CredentialStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.mark_activity_not_commute(123, 100)
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "commute": True,
                "start_latlng": [39.70, -105.10],
                "end_latlng": [39.70, -105.10],
                "distance": 7_242.048,
                "description": "Manually tagged commute",
            },
            300: {
                "id": 300,
                "sport_type": "Ride",
                "commute": False,
                "start_latlng": [39.70, -105.10],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "start_date_local": "2026-09-17T12:00:00Z",
                "distance": 1609.344,
                "description": "Errand",
            },
        },
        summaries=[
            {"id": 100, "start_date": "2026-09-17T08:00:00Z"},
            {"id": 300, "start_date": "2026-09-17T12:00:00Z"},
        ],
    )
    caplog.set_level(logging.INFO, logger="commuter.commute")

    result = await synchronize_commutes(
        store=store,
        token_manager=FakeTokenManager(),
        strava_client=client,
        notifier=FakeNotifier(),
        recheck_non_matches=True,
        verbose=True,
    )

    assert result.updated_activity_ids == [100]
    assert result.non_matching_activity_ids == [300]
    assert store.get_commute_configuration(123).cumulative_savings_cents == 78
    assert client.updates[0][0] == 100
    assert client.updates[0][1]["commute"] is True
    assert "activity=100 matches: already marked as a Strava commute" in caplog.text
    assert (
        "activity=300 date=2026-09-17 type=Ride distance=1.00mi "
        "does not match: endpoints do not connect any two configured locations"
    ) in caplog.text


@pytest.mark.asyncio
async def test_sync_matches_a_ride_between_any_two_configured_locations(store_with_gym: CredentialStore) -> None:
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "start_latlng": [GYM.latitude, GYM.longitude],
                "end_latlng": [WORK.latitude, WORK.longitude],
                "distance": 4_828.032,
                "description": "Gym to work",
            },
        },
        summaries=[{"id": 100, "start_date": "2026-09-22T07:00:00Z"}],
    )

    result = await synchronize_commutes(
        store=store_with_gym,
        token_manager=FakeTokenManager(),
        strava_client=client,
        notifier=FakeNotifier(),
    )

    assert result.updated_activity_ids == [100]
    assert client.updates[0][1]["commute"] is True


@pytest.mark.asyncio
async def test_a_ride_starting_and_ending_at_the_same_location_does_not_match(
    store_with_gym: CredentialStore,
) -> None:
    client = FakeActivityClient(
        details={
            100: {
                "id": 100,
                "sport_type": "Ride",
                "start_latlng": [HOME.latitude, HOME.longitude],
                "end_latlng": [HOME.latitude, HOME.longitude],
                "distance": 8_046.72,
                "description": "Loop from home",
            },
        },
        summaries=[{"id": 100, "start_date": "2026-09-22T07:00:00Z"}],
    )

    result = await synchronize_commutes(
        store=store_with_gym,
        token_manager=FakeTokenManager(),
        strava_client=client,
        notifier=FakeNotifier(),
    )

    assert result.updated_activity_ids == []
    assert result.non_matching_activity_ids == [100]
