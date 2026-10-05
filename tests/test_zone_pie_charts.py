from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def test_seven_day_zone_pies_use_activity_dates_and_exclude_all_commute_signals():
    from commuter.sheets import zone_pie_data

    def activity(identifier, day, category, duration=180, **fields):
        return {
            "activity_id": str(identifier),
            "activity_date": day,
            "category": category,
            "elapsed_s": duration,
            "status": "active",
            **fields,
        }

    activities = [
        activity(1, "2026-09-27", "run"),
        activity(2, "2026-10-03", "virtual_bike"),
        activity(3, "2026-10-02", "bike_other"),
        activity(4, "2026-10-03", "bike_commute"),
        activity(5, "2026-10-03", "bike_other", commute=True),
        activity(6, "2026-10-03", "virtual_bike", effective_commute=True),
        activity(7, "2026-09-26", "run"),
        activity(8, "2026-10-04", "run"),
        activity(9, "2026-10-03", "run", status="removed"),
        activity(10, "2026-10-03", "other"),
    ]
    zones = [
        {"activity_id": str(i), "zone": "Z2", "seconds": 120, "week": "2026-09-21"}
        for i in range(1, 11)
    ]
    totals = {r["zone"]: r for r in zone_pie_data(activities, zones, date(2026, 10, 3))}
    assert totals["Z2"]["run_minutes"] == 2
    assert totals["Z2"]["bike_minutes"] == 4
    assert totals["Z2"]["combined_minutes"] == 6
    assert totals["unknown"]["run_minutes"] == 1
    assert totals["unknown"]["bike_minutes"] == 2
    assert all(
        r["combined_minutes"] == r["run_minutes"] + r["bike_minutes"]
        for r in totals.values()
    )
    assert totals["Z5"]["combined_minutes"] == 0


def test_zone_pies_keep_missing_hr_unknown_and_do_not_double_count_unknown_rows():
    from commuter.sheets import zone_pie_data

    activities = [
        {
            "activity_id": "1",
            "activity_date": "2026-10-03",
            "category": "run",
            "elapsed_s": 300,
            "zone_availability": "missing_hr",
        },
        {
            "activity_id": "2",
            "activity_date": "2026-10-03",
            "category": "bike_other",
            "elapsed_s": 180,
        },
    ]
    zones = [
        {"activity_id": "2", "zone": "Z1", "seconds": 120},
        {"activity_id": "2", "zone": "unknown", "seconds": 60},
    ]
    totals = {r["zone"]: r for r in zone_pie_data(activities, zones, date(2026, 10, 3))}
    assert totals["unknown"]["run_minutes"] == 5
    assert totals["unknown"]["bike_minutes"] == 1
    assert sum(r["bike_minutes"] for r in totals.values()) == 3


@pytest.mark.asyncio
async def test_setup_adds_only_three_zone_pies_and_preserves_chart_deletions():
    from test_training_sync import FakeSheets

    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    sdk.tables["Dashboard"] = []
    sdk.properties["Dashboard"] = {
        "title": "Dashboard",
        "sheetId": 500,
        "gridProperties": {"rowCount": 1000, "columnCount": 26},
    }
    existing = [
        {
            "chartId": 700 + i,
            "spec": {"title": f"User chart {i}", "basicChart": {"chartType": "LINE"}},
            "position": {
                "overlayPosition": {
                    "anchorCell": {"sheetId": 500, "rowIndex": 20 * i},
                    "widthPixels": 900,
                    "heightPixels": 350,
                }
            },
        }
        for i in range(7)
    ]
    sdk.charts[500] = deepcopy(existing)
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        charts = sdk.charts[500]
        pies = [c for c in charts if "pieChart" in c["spec"]]
        assert len(pies) == 3
        assert charts[:7] == existing
        assert all("no commutes" in c["spec"]["title"] for c in pies)
        assert all(
            c["spec"]["pieChart"]["domain"]["sourceRange"]["sources"][0][
                "startRowIndex"
            ]
            == 1
            for c in pies
        )
        assert "Sport-specific" in pies[2]["spec"]["subtitle"]
        for i, pie in enumerate(pies, 1):
            assert (
                pie["spec"]["pieChart"]["series"]["sourceRange"]["sources"][0][
                    "startColumnIndex"
                ]
                == i
            )
        # A deleted pie must also stay deleted on ordinary setup/sync.
        deleted = pies[0]["chartId"]
        sdk.charts[500] = [c for c in charts if c["chartId"] != deleted]
        await adapter.setup()
        assert len(sdk.charts[500]) == 9
        assert not any(c["chartId"] == deleted for c in sdk.charts[500])
        assert sdk.charts[500][:7] == existing
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_finish_rebuilds_zone_pie_sources_and_clears_obsolete_rows():
    from test_training_sync import FakeSheets

    from commuter.sheets import ACTIVITY_COLUMNS, ZONE_COLUMNS, SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        today = datetime.now(ZoneInfo(adapter.reporting_timezone)).date()
        week = (today - timedelta(days=today.weekday())).isoformat()
        activity = {
            "activity_id": "1",
            "activity_date": today.isoformat(),
            "week": week,
            "category": "run",
            "elapsed_s": 300,
            "status": "active",
        }
        sdk.tables["Activities"].append([activity.get(c, "") for c in ACTIVITY_COLUMNS])
        zone = {
            "activity_id": "1",
            "week": week,
            "category": "run",
            "settings_version": "strava-run-v1",
            "zone": "Z2",
            "seconds": 240,
        }
        sdk.tables["Zone Time"].append([zone.get(c, "") for c in ZONE_COLUMNS])
        sdk.tables["Zone Pie Chart Data"].append(["old-zone", 999, 999, 999])
        await adapter.finish("success")
        rows = {r[0]: r[1:] for r in sdk.tables["Zone Pie Chart Data"][1:] if r[0]}
        assert rows["Z2"] == [4, 0, 4]
        assert rows["unknown"] == [1, 0, 1]
        assert "old-zone" not in rows
        activity["status"] = "removed"
        sdk.tables["Activities"][1] = [activity.get(c, "") for c in ACTIVITY_COLUMNS]
        await adapter.finish("success")
        assert all(
            sum(row[1:]) == 0 for row in sdk.tables["Zone Pie Chart Data"][1:] if row[0]
        )
    finally:
        await adapter.close()


def test_boundary_catalog_deduplicates_workouts_and_retains_sport_versions():
    from commuter.sheets import zone_boundary_data

    rows = [
        {
            "sport": "run",
            "zone": "Z2",
            "lower_bpm": 136,
            "upper_bpm": 168,
            "settings_version": "run-v1",
        },
        {
            "sport": "cycling",
            "zone": "Z2",
            "lower_bpm": 123,
            "upper_bpm": 161,
            "settings_version": "bike-v1",
        },
        {
            "sport": "run",
            "zone": "Z2",
            "lower_bpm": 140,
            "upper_bpm": 170,
            "settings_version": "run-v2",
        },
        {
            "sport": "run",
            "zone": "Z5",
            "lower_bpm": 202,
            "upper_bpm": "",
            "settings_version": "run-v1",
        },
    ]
    result = zone_boundary_data(rows + rows)
    assert len(result) == 4
    assert any(r["sport"] == "cycling" and r["lower_bpm"] == 123 for r in result)
    assert any(
        r["settings_version"] == "run-v2" and r["lower_bpm"] == 140 for r in result
    )
    assert next(r for r in result if r["zone"] == "Z5")["upper_bpm"] is None


@pytest.mark.asyncio
async def test_boundary_catalog_is_visible_in_analysis_and_preserves_user_ranges():
    from test_training_sync import FakeSheets

    from commuter.sheets import REPORTED_ZONE_COLUMNS, SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        sdk.tables["Analysis"][2] = ["User label", "=1+2"]
        while len(sdk.tables["Analysis"]) < 30:
            sdk.tables["Analysis"].append([])
        sdk.tables["Analysis"][29] = ["Preserve notes"]
        row = {
            "activity_id": "1",
            "sport": "run",
            "zone": "Z2",
            "lower_bpm": 136,
            "upper_bpm": 168,
            "settings_version": "run-v1",
        }
        sdk.tables["Strava HR Zones"].append(
            [row.get(c, "") for c in REPORTED_ZONE_COLUMNS]
        )
        await adapter.finish("success")
        assert sdk.tables["Analysis"][6][0] == "Strava HR zone boundaries"
        assert sdk.tables["Analysis"][7] == [
            "Sport",
            "Zone",
            "Lower bpm",
            "Upper bpm",
            "Settings version",
        ]
        assert sdk.tables["Analysis"][8] == ["run", "Z2", 136, 168, "run-v1"]
        assert sdk.tables["Analysis"][2] == ["User label", "=1+2"]
        assert sdk.tables["Analysis"][29] == ["Preserve notes"]
        sdk.tables["Strava HR Zones"] = sdk.tables["Strava HR Zones"][:1]
        await adapter.finish("success")
        assert not any(sdk.tables["Analysis"][8])
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_boundary_catalog_refuses_to_overwrite_unowned_analysis_cells():
    from test_training_sync import FakeSheets

    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        sdk.tables["Analysis"].append(["Existing manual note"])
        with pytest.raises(ValueError, match="would overwrite user cells"):
            await adapter.finish("success")
        assert sdk.tables["Analysis"][6] == ["Existing manual note"]
        assert not any(r[0] == "last_success_utc" for r in sdk.tables["Sync Log"][1:])
    finally:
        await adapter.close()
