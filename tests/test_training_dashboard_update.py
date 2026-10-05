from datetime import date

import pytest


def test_partial_windows_recover_after_gaps_and_keep_sparse_intervals():
    from commuter.training import recording_gap_cutoff, rolling_measurements

    assert recording_gap_cutoff([0, 4, 8, 12, 50, 54]) == 12
    samples = rolling_measurements(
        [0, 1, 2, 20, 21, 22, 23],
        [100, 120, 140, 160, 180, 200, 220],
        [0, 2, 4, 40, 42, 44, 46],
        [True] * 7,
        3,
    )
    assert samples[1].heart_rate_bpm == 100
    assert samples[1].pace_s_km == 500
    assert samples[2].heart_rate_bpm is None  # Following long gap stays unknown.
    assert samples[3].heart_rate_bpm is None
    assert samples[4].heart_rate_bpm == 160
    assert samples[5].heart_rate_bpm == 170
    sparse = rolling_measurements(
        [0, 4, 8], [120, 150, 160], [0, 16, 32], [True] * 3, 12
    )
    assert sparse[1].heart_rate_bpm == 120
    assert sparse[1].pace_s_km == 250


def test_twelve_week_charts_and_daily_seven_day_totals_exclude_commutes():
    from commuter.sheets import dashboard_data

    def row(day, category, minutes, **kw):
        return {
            "activity_date": day,
            "category": category,
            "moving_s": minutes * 60,
            "status": "active",
            **kw,
        }

    rows = [
        row("2026-07-17", "run", 10),
        row("2026-07-20", "run", 20),
        row("2026-10-01", "run", 30),
        row("2026-10-02", "virtual_bike", 40),
        row("2026-10-03", "bike_other", 50),
        row("2026-10-03", "bike_commute", 900),
        row("2026-10-03", "other", 11),
        row("2026-10-03", "run", 200, status="removed"),
        row("2026-10-03", "bike_other", 400, effective_commute=True),
    ]
    weekly, daily = dashboard_data(rows, date(2026, 10, 3))
    assert len(weekly) == 12
    assert weekly[0]["week"] == "2026-07-13"
    assert weekly[-1]["non_commute_minutes"] == 131
    assert daily[-1]["run_7d_minutes"] == 30
    assert daily[-1]["bike_7d_minutes"] == 90
    # Include seed days before the chart range in the first rolling total.
    assert daily[0]["run_7d_minutes"] == 0
    assert daily[4]["run_7d_minutes"] == 10


@pytest.mark.asyncio
async def test_native_running_and_cycling_zone_totals_export_without_personal_thresholds():
    from commuter.models import ActivityEnvelope
    from commuter.pipeline import ProcessingContext
    from commuter.training import TrainingSheetsProcessor

    for sport, category in [
        ("Run", "run"),
        ("Ride", "bike_other"),
        ("VirtualRide", "virtual_bike"),
    ]:
        activity = ActivityEnvelope(
            1,
            2,
            {
                "id": 2,
                "sport_type": sport,
                "start_date": "2026-10-01T12:00:00Z",
                "elapsed_time": 120,
            },
        )
        activity.reported_zones = [
            {
                "type": "heartrate",
                "sensor_based": True,
                "distribution_buckets": [
                    {"min": 0, "max": 135, "time": 30},
                    {"min": 136, "max": -1, "time": 60},
                ],
            }
        ]
        await TrainingSheetsProcessor().process(
            ProcessingContext(dry_run=True), activity
        )
        assert activity.training["zone_availability"] == "strava_reported"
        assert activity.training["classified_s"] == 90
        assert activity.training["category"] == category
        assert activity.training["zones"][0]["seconds"] == 30
        assert activity.training["reported_zones"][1]["upper_bpm"] is None
        assert activity.training["zones"][0]["method_version"] == "strava-reported-v1"


@pytest.mark.asyncio
async def test_dashboard_setup_replaces_pace_with_requested_duration_charts():
    from test_training_sync import FakeSheets

    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        charts = sdk.charts[sdk.properties["Dashboard"]["sheetId"]]
        titles = [c["spec"]["title"] for c in charts]
        assert len(charts) == 10
        assert not any("pace" in title.lower() for title in titles)
        assert any(
            "non-commute" in title.lower() and "Weekly" in title for title in titles
        )
        assert sum("Rolling 7-day" in title for title in titles) == 2
        assert all(
            "12 weeks" in c["spec"]["title"]
            for c in charts
            if "basicChart" in c["spec"]
        )
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_weekly_non_commute_chart_labels_minutes_only_on_requested_chart():
    from test_training_sync import FakeSheets

    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        charts = sdk.charts[sdk.properties["Dashboard"]["sheetId"]]
        target = next(
            chart
            for chart in charts
            if chart["spec"]["title"] == "Weekly non-commute minutes (last 12 weeks)"
        )
        series = target["spec"]["basicChart"]["series"]
        assert len(series) == 1
        assert series[0]["dataLabel"] == {"type": "DATA"}
        assert series[0]["series"]["sourceRange"]["sources"][0]["startColumnIndex"] == 6
        assert any(
            request.get("repeatCell", {})
            .get("cell", {})
            .get("userEnteredFormat", {})
            .get("numberFormat")
            == {"type": "NUMBER", "pattern": "0.0"}
            and request["repeatCell"]["range"] == {
                "sheetId": sdk.properties["Chart Data"]["sheetId"],
                "startRowIndex": 1,
                "startColumnIndex": 6,
                "endColumnIndex": 7,
            }
            for body in sdk.writes
            for request in body.get("requests", [])
        )
        assert all(
            "dataLabel" not in series
            for chart in charts
            if chart is not target
            for series in chart["spec"].get("basicChart", {}).get("series", [])
        )
    finally:
        await adapter.close()


def test_weekly_duration_stack_reconciles_all_non_commute_categories():
    from commuter.sheets import dashboard_data

    rows = [
        {"activity_date": "2026-08-24", "category": category, "moving_s": minutes * 60}
        for category, minutes in [
            ("run", 157),
            ("virtual_bike", 386),
            ("bike_other", 139),
            ("other", 110),
            ("bike_commute", 20),
        ]
    ]
    weekly, _ = dashboard_data(rows, date(2026, 10, 3))
    week = next(row for row in weekly if row["week"] == "2026-08-24")
    assert week["bike_other_minutes"] == 139
    assert week["other_minutes"] == 110
    stack = sum(
        week[f"{category}_minutes"]
        for category in ("run", "virtual_bike", "bike_other", "other", "bike_commute")
    )
    assert stack == week["non_commute_minutes"] + week["bike_commute_minutes"] == 812


@pytest.mark.asyncio
async def test_duration_chart_categories_and_total_labels_after_schema_upgrade():
    from test_training_sync import FakeSheets

    from commuter.sheets import CHART_COLUMNS, SheetsAdapter

    sdk = FakeSheets()
    legacy = list(CHART_COLUMNS[:7])
    sdk.tables["Chart Data"] = [legacy, ["2026-08-24", 157, 386, 20, 18, "", 792]]
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        assert sdk.tables["Chart Data"][0] == legacy + [
            "bike_other_minutes", "other_minutes"
        ]
        assert sdk.tables["Chart Data"][1][:7] == ["2026-08-24", 157, 386, 20, 18, "", 792]
        charts = sdk.charts[sdk.properties["Dashboard"]["sheetId"]]
        target = next(
            c for c in charts
            if c["spec"]["title"].startswith("Weekly duration by category")
        )
        basic = target["spec"]["basicChart"]
        assert basic["totalDataLabel"] == {"type": "DATA"}
        assert [
            s["series"]["sourceRange"]["sources"][0]["startColumnIndex"]
            for s in basic["series"]
        ] == [1, 2, 3, 7, 8]
        assert not any("dataLabel" in s for s in basic["series"])
        assert not any(
            "totalDataLabel" in c["spec"].get("basicChart", {})
            for c in charts if c is not target
        )
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_unavailable_strava_zone_feature_is_explicit():
    import httpx

    from commuter.config import Settings
    from commuter.strava import StravaClient

    async def handler(request):
        assert request.url.path.endswith("/activities/123/zones")
        return httpx.Response(402, json={"message": "Payment Required"})

    client = StravaClient(
        Settings(
            "c", "s", __import__("pathlib").Path("/tmp/not-used"), "http://localhost"
        )
    )
    client._session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.get_activity_zones("token", 123)
    assert result == {"availability": "subscription_required", "zones": []}
    await client._session.aclose()


@pytest.mark.asyncio
async def test_extended_backfill_keeps_original_range_and_reuses_cached_sources(
    tmp_path,
):
    from datetime import datetime, timezone

    from test_training_sync import Sink, Source, Tokens, connected_store

    from commuter.config import Settings
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    store, source, sink = connected_store(tmp_path), Source(), Sink()
    cache = SourceCache(tmp_path / "cache")
    original = {"after": 123, "before": 456, "complete": True}
    store.save_progress(123, "sheet", "history", original)
    cache.save(
        123,
        1,
        {
            "detail": {
                "id": 1,
                "sport_type": "Run",
                "start_date": "2026-09-25T12:00:00Z",
            },
            "streams": {},
        },
    )
    try:
        result = await synchronize_training(
            settings=Settings("c", "s", tmp_path / "db", "http://localhost"),
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=cache,
            mode="backfill",
            months=3,
            max_activities=10,
            now=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )
        assert not result.errors
        assert source.detail_calls == [2]
        assert store.get_progress(123, "sheet", "history") == original
        assert store.get_progress(123, "sheet", "history_3m")["complete"]
        assert "reported_zones" in cache.load(123, 1)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_analysis_zone_status_recognizes_platform_exports():
    from test_training_sync import FakeSheets

    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        formula = sdk.tables["Analysis"][5][1]
        assert "strava_reported" in formula
        assert "personal method unreviewed" in formula
    finally:
        await adapter.close()
