from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


def test_three_second_windows_are_time_weighted_and_do_not_extrapolate() -> None:
    from commuter.training import rolling_measurements

    result = rolling_measurements(
        [0, 1, 2, 3, 4], [100, 130, 160, 190, 190], [0, 2, 4, 10, 16], [True] * 5, 10
    )
    assert [item.heart_rate_bpm for item in result] == [None, 100, 115, 130, 160]
    assert result[3].pace_s_km == pytest.approx(300)
    assert result[4].pace_s_km == pytest.approx(3000 / 14)


def test_rolling_windows_use_seconds_not_sample_counts() -> None:
    from commuter.training import rolling_measurements

    result = rolling_measurements([0, 2, 5], [100, 160, 200], [0, 4, 10], [True] * 3, 5)
    assert result[2].heart_rate_bpm == 160
    assert result[2].pace_s_km == 500


@pytest.mark.parametrize("kind", ["gap", "invalid_hr", "pause", "distance_reset"])
def test_windows_do_not_bridge_untrusted_intervals(kind: str) -> None:
    from commuter.training import rolling_measurements

    times, hr, distance, moving = (
        [0, 1, 2, 3, 4, 5, 6],
        [140] * 7,
        list(range(7)),
        [True] * 7,
    )
    if kind == "gap":
        times = [0, 1, 2, 20, 21, 22, 23]
    if kind == "invalid_hr":
        hr[2] = None
    if kind == "pause":
        moving[2] = False
    if kind == "distance_reset":
        distance[2] = 0
    result = rolling_measurements(times, hr, distance, moving, 5)
    if kind in ("gap", "invalid_hr"):
        assert result[3].heart_rate_bpm is None
    elif kind == "pause":
        assert result[3].pace_s_km is None
    else:
        assert result[2].pace_s_km is None
        assert result[3].pace_s_km == pytest.approx(1000 / 3)
    assert result[-1].heart_rate_bpm == 140
    assert result[-1].pace_s_km is not None


@pytest.mark.parametrize(
    "times,hr", [([0, 1], [120]), ([0, 0], [120, 120]), ([0, float("nan")], [120, 120])]
)
def test_malformed_streams_are_rejected(times, hr) -> None:
    from commuter.training import rolling_measurements

    with pytest.raises(ValueError):
        rolling_measurements(times, hr, None, None, 5)


def test_zones_count_rolling_values_and_unknown_startup_time() -> None:
    from commuter.models import ZoneBoundary, ZoneSettings
    from commuter.training import calculate_zones, rolling_measurements

    settings = ZoneSettings(
        "run",
        "2026-01-01",
        "test-v1",
        (ZoneBoundary("Z1", 0, 140), ZoneBoundary("Z2", 140, None)),
        True,
        5,
        "test",
    )
    samples = rolling_measurements(
        list(range(7)),
        [130, 130, 160, 160, 160, 160, 160],
        list(range(7)),
        [True] * 7,
        5,
    )
    result = calculate_zones(samples, settings)
    assert result.seconds == {"Z1": 2, "Z2": 3}
    assert result.unknown_s == 1
    assert result.classified_s + result.unknown_s == 6
    assert result.seconds["Z2"] == 3  # 140 is included in Z2 at t=3.


def test_pace_missing_distance_and_real_zero_are_distinct() -> None:
    from commuter.training import workout_pace

    assert workout_pace(0, 100) is None
    assert workout_pace(None, 100) is None
    assert workout_pace(5000, 1500) == 300


@pytest.mark.asyncio
async def test_context_first_pipeline_passes_each_updated_activity_to_next_processor() -> (
    None
):
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import Pipeline, ProcessingContext

    context = ProcessingContext()
    calls = []

    class First:
        name = "first"

        async def process(self, ctx, activity):
            assert ctx is context
            calls.append(self.name)
            activity.effective_commute = True
            return activity

    class Second:
        name = "second"

        async def process(self, ctx, activity):
            assert ctx is context and activity.effective_commute
            calls.append(self.name)
            return activity

    activity = ActivityEnvelope(123, 456, {"id": 456})
    assert await Pipeline((First(), Second())).process(context, activity) is activity
    assert calls == ["first", "second"]


def test_private_cache_atomic_replay_and_lock_contention(tmp_path: Path) -> None:
    from commuter.state import SourceCache, process_lock

    cache = SourceCache(tmp_path / "cache")
    cache.save(1, 2, {"detail": {"id": 2}, "streams": {}})
    assert cache.load(1, 2)["detail"]["id"] == 2
    assert (tmp_path / "cache" / "1" / "2.json").stat().st_mode & 0o777 == 0o600
    with process_lock(tmp_path / "run.lock"):
        with pytest.raises(ValueError, match="already running"):
            with process_lock(tmp_path / "run.lock"):
                pass


def test_two_calendar_months_clamp_short_months() -> None:
    from commuter.pipeline import history_start

    assert (
        history_start(datetime(2026, 4, 30, 12, tzinfo=timezone.utc), 2).isoformat()
        == "2026-02-28T12:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_training_exports_missing_hr_and_replaces_values_on_replay() -> None:
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import ProcessingContext
    from commuter.training import TrainingSheetsProcessor

    class Sheets:
        rows = []

        async def upsert(self, row, zones):
            self.rows.append((row, zones))

    sink = Sheets()
    context = ProcessingContext(sheets=sink)
    activity = ActivityEnvelope(
        1,
        2,
        {
            "id": 2,
            "sport_type": "Run",
            "start_date": "2026-10-01T12:00:00Z",
            "distance": 5000,
            "moving_time": 1500,
            "elapsed_time": 1600,
        },
        streams={},
    )
    await TrainingSheetsProcessor().process(context, activity)
    row = sink.rows[-1][0]
    assert row["activity_id"] == "2"
    assert row["moving_pace_s_km"] == 300
    assert row["zone_availability"] == "missing_hr"
    activity.source["distance"] = None
    await TrainingSheetsProcessor().process(context, activity)
    assert sink.rows[-1][0]["moving_pace_s_km"] is None


def test_weekly_summary_weights_pace_and_preserves_missing_hr_coverage() -> None:
    from commuter.sheets import summarize

    rows = [
        {
            "activity_id": "1",
            "week": "2026-09-28",
            "category": "run",
            "moving_s": 300,
            "elapsed_s": 310,
            "distance_m": 1000,
            "moving_pace_s_km": 300,
            "zone_availability": "available",
            "settings_version": "v1",
            "classified_s": 300,
            "unknown_s": 10,
        },
        {
            "activity_id": "2",
            "week": "2026-09-28",
            "category": "run",
            "moving_s": 1200,
            "elapsed_s": 1210,
            "distance_m": 2000,
            "moving_pace_s_km": 600,
            "zone_availability": "missing_hr",
            "settings_version": "v1",
        },
    ]
    summary, zones = summarize(rows, [])
    assert summary[0]["moving_pace_s_km"] == 500
    assert summary[0]["missing_hr_count"] == 1
    assert summary[0]["activities"] == 2


def test_stage_state_is_independent_of_existing_commute_state(tmp_path: Path) -> None:
    from commuter.store import CredentialStore

    store = CredentialStore(tmp_path / "state.db")
    try:
        store.mark_activity_not_commute(1, 2)
        store.save_training_state(1, 2, "sheet", "uploaded", "source", "settings")
        assert store.get_activity_processing(1, 2).status == "not_commute"
        assert store.get_training_state(1, 2, "sheet")["status"] == "uploaded"
        store.save_progress(
            1, "sheet", "history", {"after": 100, "before": 200, "page": 3}
        )
        assert store.get_progress(1, "sheet", "history")["after"] == 100
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rolling_pace_does_not_require_hr_or_zone_thresholds() -> None:
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import ProcessingContext
    from commuter.training import TrainingSheetsProcessor

    activity = ActivityEnvelope(
        1,
        2,
        {"id": 2, "sport_type": "Run", "start_date": "2026-10-01T12:00:00Z"},
        streams={
            "time": {"data": list(range(7))},
            "distance": {"data": [i * 4 for i in range(7)]},
            "moving": {"data": [True] * 7},
        },
    )
    await TrainingSheetsProcessor().process(
        ProcessingContext(dry_run=True, max_gap_s=5), activity
    )
    assert activity.training["rolling_pace_s_km"] == 250
    assert activity.training["zone_availability"] == "missing_hr"


@pytest.mark.asyncio
async def test_measurements_export_before_zone_settings_with_documented_gap_default() -> (
    None
):
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import ProcessingContext
    from commuter.training import TrainingSheetsProcessor

    activity = ActivityEnvelope(
        1,
        2,
        {"id": 2, "sport_type": "Run", "start_date": "2026-10-01T12:00:00Z"},
        streams={
            "time": {"data": list(range(7))},
            "heartrate": {"data": [140] * 7},
            "distance": {"data": [i * 4 for i in range(7)]},
            "moving": {"data": [True] * 7},
        },
    )
    await TrainingSheetsProcessor().process(ProcessingContext(dry_run=True), activity)
    assert activity.training["rolling_pace_s_km"] == 250
    assert activity.training["rolling_hr_mean_bpm"] == 140
    assert activity.training["recording_gap_cutoff_s"] == 3
    assert activity.training["zone_availability"] == "missing_thresholds"


def test_cache_restricts_permissions_of_preexisting_owned_directories(tmp_path) -> None:
    from commuter.state import SourceCache

    root = tmp_path / "cache"
    root.mkdir(mode=0o755)
    cache = SourceCache(root)
    cache.save(1, 2, {"detail": {}, "streams": {}})
    assert root.stat().st_mode & 0o777 == 0o700


def test_existing_home_work_configuration_remains_readable_with_pipeline() -> None:
    import json

    from commuter.store import _deserialize_commute_configuration

    config = _deserialize_commute_configuration(
        123,
        json.dumps(
            {
                "home": {"latitude": 40.0, "longitude": -105.0},
                "work": {"latitude": 40.1, "longitude": -105.1},
                "radius_m": 150,
                "combined_mpg": 25,
                "gas_price_cents": 434,
                "vehicle_name": "car",
                "currency": "USD",
            }
        ),
        1200,
        27000,
        100,
    )
    assert [location.name for location in config.locations] == ["home", "work"]
    assert config.locations[1].coordinate.latitude == 40.1
    assert config.cumulative_savings_cents == 1200
    assert config.cumulative_co2_avoided_grams == 27000
    assert config.created_at == 100


@pytest.mark.asyncio
async def test_untrusted_recordings_do_not_claim_available_rolling_measurements() -> (
    None
):
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import ProcessingContext
    from commuter.training import TrainingSheetsProcessor

    activity = ActivityEnvelope(
        1,
        2,
        {"id": 2, "sport_type": "Run", "start_date": "2026-10-01T12:00:00Z"},
        streams={
            "time": {"data": [0, 10, 20]},
            "heartrate": {"data": [140, 140, 140]},
            "distance": {"data": [0, 40, 80]},
            "moving": {"data": [True] * 3},
        },
    )
    await TrainingSheetsProcessor().process(
        ProcessingContext(dry_run=True, max_gap_s=3), activity
    )
    assert activity.training["rolling_hr_mean_bpm"] is None
    assert activity.training["rolling_pace_s_km"] is None
    assert activity.training["rolling_availability"] == "insufficient_trusted_coverage"
