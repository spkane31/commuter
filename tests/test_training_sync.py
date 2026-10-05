from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from commuter.config import Settings
from commuter.models import Athlete, TokenSet
from commuter.store import CredentialStore


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self, **kwargs):
        return self.action()


class FakeSheets:
    def __init__(self):
        from commuter.sheets import TABLES, ZONE_CHART_TABS

        self.tables = {name: [list(columns)] for name, columns in TABLES.items()}
        self.tables.update(
            {
                title: [["week_and_settings", "unknown"]]
                for title in ZONE_CHART_TABS.values()
            }
        )
        self.properties = {
            name: {
                "title": name,
                "sheetId": i,
                "gridProperties": {
                    "rowCount": 1000,
                    "columnCount": max(26, len(rows[0])),
                },
            }
            for i, (name, rows) in enumerate(self.tables.items())
        }
        self.charts = {}
        self.writes = []

    def values(self):
        return self

    def get(self, *, spreadsheetId, range=None, **kwargs):
        if range is None:
            return Request(
                lambda: {
                    "sheets": [
                        {"properties": p, "charts": self.charts.get(p["sheetId"], [])}
                        for p in self.properties.values()
                    ]
                }
            )
        title = range.split("!")[0].strip("'")

        def read():
            rows = [list(r) for r in self.tables[title]]
            block = re.search(r"!A(\d+):E(\d+)$", range)
            if block:
                return {
                    "values": [r[:5] for r in rows[int(block[1]) - 1 : int(block[2])]]
                }
            if range.endswith("!B6"):
                return {
                    "values": [[rows[5][1]]]
                    if len(rows) > 5 and len(rows[5]) > 1
                    else []
                }
            if range.endswith("1:1") or re.search(r"!A1(?::[A-Z]+1)?$", range):
                rows = rows[:1]
            return {"values": rows}

        return Request(read)

    def batchGet(self, *, spreadsheetId, ranges, **kwargs):
        return Request(
            lambda: {
                "valueRanges": [
                    self.get(spreadsheetId=spreadsheetId, range=r).execute()
                    for r in ranges
                ]
            }
        )

    def batchUpdate(self, *, spreadsheetId, body):
        def update():
            self.writes.append(body)
            if "requests" in body:
                replies = []
                for request in body["requests"]:
                    if "addSheet" in request:
                        name = request["addSheet"]["properties"]["title"]
                        self.tables[name] = []
                        self.properties[name] = {
                            "title": name,
                            "sheetId": len(self.properties),
                            "gridProperties": {"rowCount": 1000, "columnCount": 26},
                        }
                    elif "updateSheetProperties" in request:
                        props = request["updateSheetProperties"]["properties"]
                        current = next(
                            p
                            for p in self.properties.values()
                            if p["sheetId"] == props["sheetId"]
                        )
                        current["gridProperties"].update(
                            props.get("gridProperties", {})
                        )
                    elif "addChart" in request:
                        chart = request["addChart"]["chart"] | {
                            "chartId": sum(len(v) for v in self.charts.values()) + 100
                        }
                        sheet = chart["position"]["overlayPosition"]["anchorCell"][
                            "sheetId"
                        ]
                        self.charts.setdefault(sheet, []).append(chart)
                        replies.append({"addChart": {"chart": chart}})
                    elif "deleteEmbeddedObject" in request:
                        identifier = request["deleteEmbeddedObject"]["objectId"]
                        for key in self.charts:
                            self.charts[key] = [
                                c
                                for c in self.charts[key]
                                if c["chartId"] != identifier
                            ]
                return {"replies": replies}
            for entry in body["data"]:
                title, cell = entry["range"].split("!")
                title = title.strip("'")
                start = int(re.search(r"\d+", cell).group()) - 1
                end_col = re.search(r":([A-Z]+)", cell)
                if end_col:
                    column = 0
                    for letter in end_col.group(1):
                        column = column * 26 + ord(letter) - 64
                    assert (
                        column
                        <= self.properties[title]["gridProperties"]["columnCount"]
                    )
                assert (
                    start + len(entry["values"])
                    <= self.properties[title]["gridProperties"]["rowCount"]
                )
                for offset, row in enumerate(entry["values"]):
                    while len(self.tables[title]) <= start + offset:
                        self.tables[title].append([])
                    self.tables[title][start + offset] = row
            return {}

        return Request(update)


@pytest.mark.asyncio
async def test_sheet_upsert_survives_sort_and_clears_obsolete_zone_rows() -> None:
    from commuter.sheets import ACTIVITY_COLUMNS, SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.upsert(
            {"activity_id": "1", "name": "=literal", "average_hr_bpm": 130},
            [
                {"activity_id": "1", "zone": "Z1", "seconds": 20},
                {"activity_id": "1", "zone": "Z2", "seconds": 10},
            ],
        )
        await adapter.upsert({"activity_id": "2", "name": "second"}, [])
        sdk.tables["Activities"][1:] = reversed(sdk.tables["Activities"][1:])
        await adapter.upsert({"activity_id": "1", "name": "edited"}, [])
        index = ACTIVITY_COLUMNS.index("average_hr_bpm")
        assert sdk.tables["Activities"][2][index] == ""
        assert len(sdk.tables["Activities"]) == 3
        assert all(not row[0] for row in sdk.tables["Zone Time"][1:])
        assert all(body["valueInputOption"] == "RAW" for body in sdk.writes)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_summary_failure_does_not_publish_success_marker() -> None:
    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    original = adapter._write

    def fail_summary(ranges, *args):
        if any("Weekly Summary" in r["range"] for r in ranges):
            raise RuntimeError("outage")
        return original(ranges, *args)

    try:
        await adapter.upsert(
            {
                "activity_id": "1",
                "week": "2026-09-28",
                "category": "run",
                "status": "active",
            },
            [],
        )
        adapter._write = fail_summary
        with pytest.raises(RuntimeError):
            await adapter.finish("success")
        assert sdk.tables["Sync Log"] == [["key", "value"]]
    finally:
        await adapter.close()


class Source:
    def __init__(self):
        self.list_calls = []
        self.detail_calls = []
        self.fail = False

    async def list_athlete_activities(
        self, token, *, after, before=None, page=1, per_page=100
    ):
        self.list_calls.append((after, before, page))
        if page > 1:
            return []
        return [
            {"id": 1, "start_date": "2026-09-25T12:00:00Z"},
            {"id": 2, "start_date": "2026-09-26T12:00:00Z"},
        ]

    async def get_activity(self, token, identifier):
        self.detail_calls.append(identifier)
        if self.fail:
            raise ValueError("details unavailable")
        return {
            "id": identifier,
            "sport_type": "Run",
            "start_date": f"2026-09-{24 + identifier}T12:00:00Z",
            "distance": 5000,
            "moving_time": 1500,
            "elapsed_time": 1500,
        }

    async def get_activity_streams(self, token, identifier):
        return {}

    async def get_activity_zones(self, token, identifier):
        return {"zones": []}


class Tokens:
    async def get_access_token(self, athlete):
        return "token"


class Sink:
    workbook = "sheet"

    def __init__(self):
        self.rows = {}
        self.fail = False
        self.finishes = []

    async def snapshot_settings(self):
        return (), {}

    async def upsert(self, row, zones):
        if self.fail:
            raise RuntimeError("unavailable")
        self.rows[row["activity_id"]] = row

    async def finish(self, status, details=""):
        self.finishes.append(status)

    async def reconcile(self, present, after, before, **kwargs):
        pass


def connected_store(tmp_path):
    store = CredentialStore(tmp_path / "commuter.db")
    store.save_account(
        Athlete(123, "test"),
        {"activity:read_all"},
        TokenSet("access", "refresh", 9999999999, None),
    )
    return store


@pytest.mark.asyncio
async def test_backfill_resumes_fixed_range_after_partial_batch(tmp_path: Path) -> None:
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    store, source, sink = connected_store(tmp_path), Source(), Sink()
    settings = Settings(
        "client",
        "secret",
        tmp_path / "commuter.db",
        "http://localhost",
        reporting_timezone="UTC",
    )
    try:
        first = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="backfill",
            max_activities=1,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        progress = store.get_progress(123, "sheet", "history")
        assert first.pending
        assert progress["after"] == int(
            datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
        )
        second = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="backfill",
            max_activities=10,
            now=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        assert not second.pending
        assert set(sink.rows) == {"1", "2"}
        assert (
            store.get_progress(123, "sheet", "history")["before"] == progress["before"]
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_sheet_export_is_pending_and_cached_recalculate_uses_no_strava(
    tmp_path: Path,
) -> None:
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    store, source, sink = connected_store(tmp_path), Source(), Sink()
    settings = Settings(
        "client",
        "secret",
        tmp_path / "commuter.db",
        "http://localhost",
        reporting_timezone="UTC",
    )
    sink.fail = True
    try:
        result = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="refresh",
            activity_id=1,
        )
        assert (
            result.errors
            and store.get_training_state(123, 1, "sheet")["status"] == "failed"
        )
        sink.fail = False
        source.fail = True
        source.detail_calls.clear()
        result = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="recalculate",
        )
        assert result.exported == [1]
        assert source.detail_calls == []
        assert store.get_training_state(123, 1, "sheet")["status"] == "completed"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dry_run_training_does_not_write_cache_state_or_sheet(
    tmp_path: Path,
) -> None:
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    store, source, sink = connected_store(tmp_path), Source(), Sink()
    settings = Settings(
        "client",
        "secret",
        tmp_path / "commuter.db",
        "http://localhost",
        reporting_timezone="UTC",
    )
    try:
        result = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="backfill",
            dry_run=True,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result.exported == [1, 2]
        assert not sink.rows and not sink.finishes
        assert not (tmp_path / "cache").exists()
        assert store.get_progress(123, "sheet", "history") is None
    finally:
        store.close()


def test_chart_data_groups_weekly_categories_without_duplicating_weeks() -> None:
    from commuter.sheets import chart_data

    rows = [
        {"week": "2026-09-28", "category": "run", "moving_s": 600, "running_miles": 2},
        {"week": "2026-09-28", "category": "bike_commute", "moving_s": 300},
        {"week": "2026-09-28", "category": "virtual_bike", "moving_s": 900},
    ]
    result = chart_data(rows)
    assert len(result) == 1
    assert result[0]["run_minutes"] == 10
    assert result[0]["bike_commute_minutes"] == 5
    assert result[0]["virtual_bike_minutes"] == 15


@pytest.mark.asyncio
async def test_activity_streams_and_page_filters_use_the_existing_http_client(
    tmp_path, monkeypatch
) -> None:
    from commuter import strava

    calls = []

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, *, headers, params):
            calls.append((url, params))

            class Response:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return (
                        {"time": {"data": [0, 1]}} if url.endswith("/streams") else []
                    )

            return Response()

    monkeypatch.setattr(strava.httpx, "AsyncClient", lambda timeout: HTTP())
    settings = Settings("client", "secret", tmp_path / "db", "http://localhost")
    client = strava.StravaClient(settings)
    await client.list_athlete_activities("token", after=1, before=3, page=2)
    assert calls[-1][1]["page"] == 2 and calls[-1][1]["before"] == 3
    assert await client.get_activity_streams("token", 1) == {"time": {"data": [0, 1]}}
    assert calls[-1][1]["keys"] == "time,heartrate,moving,distance"


@pytest.mark.asyncio
async def test_sheet_retry_never_repeats_completed_commute_mutation(
    tmp_path, monkeypatch
) -> None:
    import commuter.discord
    from commuter.models import CommuteConfiguration, Coordinate, Location
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    notifications, mutations = [], []

    class Notifier:
        def __init__(self, settings):
            pass

        async def activity_updated(self, **kwargs):
            notifications.append(kwargs["activity_id"])

    class Ride(Source):
        async def get_activity(self, token, identifier):
            return {
                "id": identifier,
                "sport_type": "Ride",
                "start_date": "2026-09-25T12:00:00Z",
                "distance": 5000,
                "start_latlng": [39, -105],
                "end_latlng": [40, -105],
            }

        async def update_activity(self, token, identifier, update):
            mutations.append(identifier)

    monkeypatch.setattr(commuter.discord, "DiscordNotifier", Notifier)
    store, source, sink = connected_store(tmp_path), Ride(), Sink()
    store.save_commute_configuration(
        CommuteConfiguration(
            123,
            (
                Location("home", Coordinate(39, -105)),
                Location("work", Coordinate(40, -105)),
            ),
            150,
            25,
            400,
            "car",
            "USD",
        )
    )
    settings = Settings(
        "client", "secret", tmp_path / "commuter.db", "http://localhost"
    )
    sink.fail = True
    try:
        await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="refresh",
            selected=("commuter", "sheets"),
            activity_id=1,
            commute_after=0,
        )
        before = store.get_commute_configuration(123).cumulative_savings_cents
        sink.fail = False
        await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="refresh",
            selected=("commuter", "sheets"),
            activity_id=1,
            commute_after=0,
        )
        assert mutations == [1] and notifications == [1]
        assert store.get_commute_configuration(123).cumulative_savings_cents == before
        assert sink.rows["1"]["category"] == "bike_commute"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_training_cache_is_removed_by_wipe(tmp_path) -> None:
    from commuter.state import SourceCache
    from commuter.wipe import wipe_local_state

    settings = Settings(
        "client", "secret", tmp_path / "commuter.db", "http://localhost"
    )
    cache = SourceCache(settings.cache_directory)
    cache.save(123, 1, {"detail": {"id": 1}, "streams": {}})
    await wipe_local_state(settings, Source(), force_local=True)
    assert not settings.cache_directory.exists()


@pytest.mark.asyncio
async def test_setup_creates_separate_zone_charts_and_preserves_manual_inputs() -> None:
    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    sdk.tables["Manual Inputs"].append(["123", "run", "", "keep this note", ""])
    sdk.properties["Activities"]["gridProperties"]["columnCount"] = 26
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        assert sdk.tables["Manual Inputs"][1][3] == "keep this note"
        dashboard = sdk.properties["Dashboard"]["sheetId"]
        charts = sdk.charts[dashboard]
        assert len(charts) == 10
        assert (
            sum(
                "HR zone" in c["spec"]["title"] and "Weekly" in c["spec"]["title"]
                for c in charts
            )
            == 2
        )
        await adapter.setup()
        assert len(sdk.charts[dashboard]) == 10
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_row_allocation_expands_grid_and_success_follows_summaries() -> None:
    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    sdk.properties["Activities"]["gridProperties"]["rowCount"] = 1
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.setup()
        await adapter.upsert(
            {
                "activity_id": "1",
                "start_utc": "2026-10-01T12:00:00+00:00",
                "week": "2026-09-28",
                "category": "run",
                "status": "active",
                "moving_s": 600,
            },
            [],
        )
        await adapter.finish("success")
        assert sdk.properties["Activities"]["gridProperties"]["rowCount"] >= 2
        assert any(row[0] == "last_success_utc" for row in sdk.tables["Sync Log"][1:])
        assert len(sdk.tables["Chart Data"]) == 13
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_configured_raw_data_tab_receives_exports_and_formulas() -> None:
    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    sdk.tables["RAW DATA"] = sdk.tables.pop("Activities")
    sdk.properties["RAW DATA"] = sdk.properties.pop("Activities")
    sdk.properties["RAW DATA"]["title"] = "RAW DATA"
    adapter = SheetsAdapter(
        "sheet", activity_sheet_name="RAW DATA", service_factory=lambda: sdk
    )
    try:
        await adapter.setup()
        await adapter.upsert({"activity_id": "1", "name": "run"}, [])
        assert "Activities" not in sdk.tables
        assert sdk.tables["RAW DATA"][1][0] == "1"
        assert "'RAW DATA'!" in sdk.tables["Analysis"][1][1]
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_reconciliation_preserves_confirmed_removal() -> None:
    from commuter.sheets import SheetsAdapter

    sdk = FakeSheets()
    adapter = SheetsAdapter("sheet", service_factory=lambda: sdk)
    try:
        await adapter.upsert(
            {
                "activity_id": "1",
                "start_utc": "2026-10-01T12:00:00Z",
                "status": "removed",
            },
            [],
        )
        await adapter.reconcile({"1"}, 0, 2000000000)
        assert adapter._read("Activities")[0]["status"] == "removed"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_training_only_does_not_require_readable_commute_configuration(
    tmp_path, monkeypatch
) -> None:
    from commuter.pipeline import synchronize_training
    from commuter.state import SourceCache

    store = connected_store(tmp_path)

    def unreadable(_):
        raise ValueError("Stored commute configuration is invalid")

    monkeypatch.setattr(store, "get_commute_configuration", unreadable)
    try:
        result = await synchronize_training(
            settings=Settings(
                "client", "secret", tmp_path / "commuter.db", "http://localhost"
            ),
            store=store,
            token_manager=Tokens(),
            strava_client=Source(),
            sheets=Sink(),
            cache=SourceCache(tmp_path / "cache"),
            mode="backfill",
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert not result.errors
        assert result.exported == [1, 2]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_wipe_refuses_to_delete_state_during_sync(tmp_path) -> None:
    from commuter.state import process_lock
    from commuter.wipe import wipe_local_state

    settings = Settings(
        "client", "secret", tmp_path / "commuter.db", "http://localhost"
    )
    store = connected_store(tmp_path)
    store.close()
    with process_lock(settings.database_path.with_suffix(".sync.lock")):
        with pytest.raises(ValueError, match="already running"):
            await wipe_local_state(settings, Source(), force_local=True)
    assert settings.database_path.exists()


@pytest.mark.asyncio
async def test_processing_errors_report_safe_integration_reason(tmp_path) -> None:
    from commuter.pipeline import synchronize_training
    from commuter.sheets import SheetsError
    from commuter.state import SourceCache

    store, sink = connected_store(tmp_path), Sink()

    async def unavailable(row, zones):
        raise SheetsError(
            "Google Sheets request failed (HTTP 429); export remains pending"
        )

    sink.upsert = unavailable
    try:
        result = await synchronize_training(
            settings=Settings(
                "client", "secret", tmp_path / "commuter.db", "http://localhost"
            ),
            store=store,
            token_manager=Tokens(),
            strava_client=Source(),
            sheets=sink,
            cache=SourceCache(tmp_path / "cache"),
            mode="backfill",
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result.pending
        assert any(
            "stage=sheets" in error and "HTTP 429" in error for error in result.errors
        )
    finally:
        store.close()


def test_partial_sync_does_not_publish_success_through_followup(
    monkeypatch, tmp_path
) -> None:
    import argparse

    import commuter.main as cli
    import commuter.pipeline as pipeline
    import commuter.sheets as sheets

    store = connected_store(tmp_path)
    store.save_progress(123, "sheet", "history", {"complete": True, "after": 0})
    store.close()
    calls = []

    async def pending(**kwargs):
        calls.append(kwargs["mode"])
        return pipeline.TrainingSyncResult(pending=True)

    class Adapter(Sink):
        def __init__(self, *args, **kwargs):
            super().__init__()

        async def close(self):
            pass

    monkeypatch.setattr(pipeline, "synchronize_training", pending)
    monkeypatch.setattr(sheets, "SheetsAdapter", Adapter)
    arguments = argparse.Namespace(
        command="sync",
        max_activities=5,
        dry_run=False,
        verbose=False,
        backfill_days=None,
        recheck_non_matches=False,
    )
    cli._training_command(
        arguments,
        argparse.ArgumentParser(),
        settings=Settings(
            "client",
            "secret",
            tmp_path / "commuter.db",
            "http://localhost",
            spreadsheet_id="sheet",
        ),
    )
    assert calls == ["sync"]
