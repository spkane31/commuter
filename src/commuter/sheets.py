"""Official Google Sheets adapter with literal writes and stable identity upserts."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from commuter.models import ZoneBoundary, ZoneSettings
from commuter.training import CATEGORIES, number, validate_zones

logger = logging.getLogger(__name__)


def _retry_after_seconds(response) -> float:
    value = next(
        (v for k, v in response.items() if k.lower() == "retry-after"), None
    )
    if not isinstance(value, str):
        return 0.0
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            seconds = float(value)
        else:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                return 0.0
            seconds = max(0.0, retry_at.timestamp() - time.time())
        return seconds if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


ACTIVITY_COLUMNS = (
    "activity_id",
    "name",
    "strava_url",
    "sport_type",
    "start_utc",
    "start_local",
    "timezone",
    "activity_date",
    "week",
    "category",
    "source",
    "commute",
    "effective_commute",
    "trainer",
    "distance_m",
    "running_miles",
    "moving_s",
    "elapsed_s",
    "average_hr_bpm",
    "max_hr_bpm",
    "moving_pace_s_km",
    "elapsed_pace_s_km",
    "moving_pace_s_mile",
    "rolling_hr_mean_bpm",
    "rolling_pace_s_km",
    "rolling_window_s",
    "stream_span_s",
    "classified_s",
    "unknown_s",
    "hr_coverage",
    "workout_hr_coverage",
    "zone_availability",
    "duration_basis",
    "settings_version",
    "settings_snapshot",
    "method_version",
    "schema_version",
    "source_version",
    "retrospective",
    "status",
    "rolling_pace_time_s",
    "rolling_pace_distance_m",
    "rolling_availability",
)
ZONE_COLUMNS = (
    "activity_id",
    "week",
    "category",
    "zone",
    "seconds",
    "settings_version",
    "method_version",
    "pace_time_s",
    "pace_distance_m",
    "availability",
)
WEEK_COLUMNS = (
    "week",
    "category",
    "activities",
    "moving_s",
    "elapsed_s",
    "running_miles",
    "missing_hr_count",
    "classified_s",
    "unknown_s",
    "moving_pace_s_km",
    "settings_versions",
    "run_minutes",
    "virtual_bike_minutes",
    "bike_commute_minutes",
)
ZONE_SUMMARY_COLUMNS = (
    "week",
    "category",
    "settings_version",
    "zone",
    "seconds",
    "minutes",
    "valid_percentage",
    "pace_s_km",
)
SETTINGS_COLUMNS = (
    "sport",
    "effective_from",
    "version",
    "zone",
    "lower_bpm",
    "upper_bpm",
    "confirmed",
    "max_gap_s",
    "method_source",
    "retrospective",
)
MANUAL_COLUMNS = ("activity_id", "category", "source", "notes", "rpe")
CHART_COLUMNS = (
    "week",
    "run_minutes",
    "virtual_bike_minutes",
    "bike_commute_minutes",
    "running_miles",
    "rolling_run_pace_s_km",
    "non_commute_minutes",
    "bike_other_minutes",
    "other_minutes",
)
DAILY_COLUMNS = ("date", "run_7d_minutes", "bike_7d_minutes")
ZONE_PIE_COLUMNS = ("zone", "run_minutes", "bike_minutes", "combined_minutes")
REPORTED_ZONE_COLUMNS = (
    "activity_id",
    "activity_date",
    "week",
    "category",
    "sport",
    "zone",
    "lower_bpm",
    "upper_bpm",
    "seconds",
    "settings_version",
    "method_version",
    "sensor_based",
    "availability",
)
ZONE_CHART_TABS = {
    "run": "Run Zone Chart Data",
    "virtual_bike": "Virtual Bike Zone Chart Data",
    "bike_commute": "Commute Zone Chart Data",
}

ACTIVITY_COLUMNS = (*ACTIVITY_COLUMNS, "recording_gap_cutoff_s")

TABLES = {
    "Activities": ACTIVITY_COLUMNS,
    "Zone Time": ZONE_COLUMNS,
    "Weekly Summary": WEEK_COLUMNS,
    "Zone Summary": ZONE_SUMMARY_COLUMNS,
    "Settings": SETTINGS_COLUMNS,
    "Manual Inputs": MANUAL_COLUMNS,
    "Sync Log": ("key", "value"),
    "Chart Data": CHART_COLUMNS,
    "Daily Chart Data": DAILY_COLUMNS,
    "Strava HR Zones": REPORTED_ZONE_COLUMNS,
    "Zone Pie Chart Data": ZONE_PIE_COLUMNS,
}


def chart_data(activities: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped = defaultdict(list)
    for row in activities:
        if row.get("status", "active") == "active":
            grouped[row["week"]].append(row)
    result = []
    for week, rows in sorted(grouped.items()):
        run = [r for r in rows if r.get("category") == "run"]
        pace_time = sum(number(r.get("rolling_pace_time_s")) or 0 for r in run)
        pace_distance = sum(number(r.get("rolling_pace_distance_m")) or 0 for r in run)
        result.append(
            {
                "week": week,
                **{
                    f"{c}_minutes": sum(
                        number(r.get("moving_s")) or 0
                        for r in rows
                        if r.get("category") == c
                    )
                    / 60
                    for c in ("run", "virtual_bike", "bike_commute")
                },
                "running_miles": sum(number(r.get("running_miles")) or 0 for r in run),
                "rolling_run_pace_s_km": 1000 * pace_time / pace_distance
                if pace_distance
                else None,
            }
        )
    return result


def dashboard_data(activities: list[dict[str, object]], today: calendar_date):
    """Keep all-activity stacks; exclude commutes and hikes from training totals."""
    start = today - timedelta(days=today.weekday() + 77)
    active = [r for r in activities if r.get("status", "active") == "active"]
    weekly = {
        (start + timedelta(days=7 * i)).isoformat(): {
            "week": (start + timedelta(days=7 * i)).isoformat(),
            "run_minutes": 0,
            "virtual_bike_minutes": 0,
            "bike_commute_minutes": 0,
            "running_miles": 0,
            "non_commute_minutes": 0,
            "bike_other_minutes": 0,
            "other_minutes": 0,
        }
        for i in range(12)
    }
    days = defaultdict(lambda: [0.0, 0.0])
    for row in active:
        if not row.get("activity_date"):
            continue
        moment = calendar_date.fromisoformat(row["activity_date"])
        if moment > today:
            continue
        category = row.get("category")
        minutes = (number(row.get("moving_s")) or 0) / 60
        commute = (
            category == "bike_commute"
            or row.get("effective_commute") is True
            or row.get("commute") is True
        )
        training = not commute and row.get("sport_type") != "Hike"
        if training:
            if category == "run":
                days[moment][0] += minutes
            if category in {"virtual_bike", "bike_other"}:
                days[moment][1] += minutes
        week = (moment - timedelta(days=moment.weekday())).isoformat()
        if week in weekly:
            entry = weekly[week]
            key = f"{'bike_commute' if commute else category}_minutes"
            if key in entry:
                entry[key] += minutes
            if category == "run" and row.get("sport_type") != "Hike":
                entry["running_miles"] += number(row.get("running_miles")) or 0
            if training:
                entry["non_commute_minutes"] += minutes
    daily = []
    for offset in range((today - start).days + 1):
        day = start + timedelta(days=offset)
        totals = [
            sum(days[day - timedelta(days=i)][sport] for i in range(7))
            for sport in (0, 1)
        ]
        daily.append(
            {
                "date": day.isoformat(),
                "run_7d_minutes": totals[0],
                "bike_7d_minutes": totals[1],
            }
        )
    return list(weekly.values()), daily


BOUNDARY_HEADERS = ("Sport", "Zone", "Lower bpm", "Upper bpm", "Settings version")


def zone_boundary_data(reported_zones):
    """Show each imported sport/version/zone boundary once, retaining numeric bpm."""
    unique = {}
    for row in reported_zones:
        if row.get("sport") not in {"run", "cycling"} or not row.get("zone"):
            continue
        lower, upper = number(row.get("lower_bpm")), number(row.get("upper_bpm"))
        if lower is None:
            continue
        key = (row["sport"], row["settings_version"], row["zone"], lower, upper)
        unique[key] = {
            "sport": row["sport"],
            "zone": row["zone"],
            "lower_bpm": lower,
            "upper_bpm": upper,
            "settings_version": row["settings_version"],
        }
    return [
        unique[key]
        for key in sorted(
            unique, key=lambda key: (key[:3], key[3], key[4] or float("inf"))
        )
    ]


def zone_pie_data(activities, zones, today: calendar_date):
    """Sum the last seven dates by sport-relative zone, excluding commutes and hikes.

    Include unassigned elapsed duration and workouts missing HR as unknown.
    Combined values add matching zone labels, retaining each sport's boundaries.
    """
    first = (today - timedelta(days=6)).isoformat()
    eligible = {
        str(row["activity_id"]): row
        for row in activities
        if row.get("status", "active") == "active"
        and first <= str(row.get("activity_date", "")) <= today.isoformat()
        and row.get("category") in {"run", "virtual_bike", "bike_other"}
        and row.get("commute") is not True
        and row.get("effective_commute") is not True
        and row.get("sport_type") != "Hike"
    }
    totals = {f"Z{i}": [0.0, 0.0] for i in range(1, 6)}
    totals["unknown"] = [0.0, 0.0]
    assigned = defaultdict(float)
    for row in zones:
        identifier = str(row.get("activity_id"))
        if identifier not in eligible:
            continue
        seconds = number(row.get("seconds"))
        if seconds is None or seconds < 0:
            continue
        sport = 0 if eligible[identifier]["category"] == "run" else 1
        totals.setdefault(row["zone"], [0.0, 0.0])[sport] += seconds
        assigned[identifier] += seconds
    for identifier, activity in eligible.items():
        duration = number(activity.get("elapsed_s"))
        if duration is None:
            duration = number(activity.get("stream_span_s"))
        if duration is not None:
            sport = 0 if activity["category"] == "run" else 1
            totals["unknown"][sport] += max(0, duration - assigned[identifier])
    return [
        {
            "zone": zone,
            "run_minutes": totals[zone][0] / 60,
            "bike_minutes": totals[zone][1] / 60,
            "combined_minutes": sum(totals[zone]) / 60,
        }
        for zone in [*sorted(z for z in totals if z != "unknown"), "unknown"]
    ]


class SheetsError(RuntimeError):
    """Reporting failed; the next run can safely retry the pending operation."""


def column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def summarize(activities: list[dict[str, object]], zones: list[dict[str, object]]):
    """Rebuild current totals; ratios use matched numerators and denominators."""

    grouped = defaultdict(list)
    for activity in activities:
        if activity.get("status", "active") == "active":
            grouped[(activity["week"], activity["category"])].append(activity)
    weeks = []
    for (week, category), rows in sorted(grouped.items()):
        eligible = [r for r in rows if number(r.get("moving_pace_s_km")) is not None]
        duration = sum(number(r.get("moving_s")) or 0 for r in eligible)
        distance = sum(number(r.get("distance_m")) or 0 for r in eligible)
        moving = sum(number(r.get("moving_s")) or 0 for r in rows)
        weeks.append(
            {
                "week": week,
                "category": category,
                "activities": len(rows),
                "moving_s": moving,
                "elapsed_s": sum(number(r.get("elapsed_s")) or 0 for r in rows),
                "running_miles": sum(number(r.get("running_miles")) or 0 for r in rows),
                "missing_hr_count": sum(
                    r.get("zone_availability") == "missing_hr" for r in rows
                ),
                "classified_s": sum(number(r.get("classified_s")) or 0 for r in rows),
                "unknown_s": sum(number(r.get("unknown_s")) or 0 for r in rows),
                "moving_pace_s_km": duration * 1000 / distance if distance else None,
                "settings_versions": ",".join(
                    sorted(
                        {
                            str(r["settings_version"])
                            for r in rows
                            if r.get("settings_version")
                        }
                    )
                ),
                **{
                    f"{c}_minutes": moving / 60 if category == c else 0
                    for c in ("run", "virtual_bike", "bike_commute")
                },
            }
        )
    active = {
        str(a["activity_id"])
        for a in activities
        if a.get("status", "active") == "active"
        and a.get("sport_type") != "Hike"
    }
    totals = defaultdict(lambda: [0.0, 0.0, 0.0])
    denominators = defaultdict(float)
    for row in zones:
        if str(row.get("activity_id")) not in active:
            continue
        group = (row["week"], row["category"], row["settings_version"])
        seconds = number(row.get("seconds")) or 0
        if row["zone"] != "unknown":
            denominators[group] += seconds
        values = totals[(*group, row["zone"])]
        values[0] += seconds
        values[1] += number(row.get("pace_time_s")) or 0
        values[2] += number(row.get("pace_distance_m")) or 0
    summary = []
    for (week, category, version, zone), (seconds, pace_time, pace_distance) in sorted(
        totals.items()
    ):
        denominator = denominators[(week, category, version)]
        summary.append(
            {
                "week": week,
                "category": category,
                "settings_version": version,
                "zone": zone,
                "seconds": seconds,
                "minutes": seconds / 60,
                "valid_percentage": seconds / denominator
                if denominator and zone != "unknown"
                else None,
                "pace_s_km": 1000 * pace_time / pace_distance
                if pace_distance
                else None,
            }
        )
    return weeks, summary


class SheetsAdapter:
    """Serialize all SDK operations in one thread owning its authenticated transport."""

    def __init__(
        self,
        workbook: str,
        credentials_path: Path | None = None,
        *,
        activity_sheet_name: str = "Activities",
        service_factory=None,
        reporting_timezone: str = "America/Denver",
    ) -> None:
        if not workbook:
            raise ValueError("COMMUTER_SPREADSHEET_ID is required")
        if (
            not activity_sheet_name
            or any(c in activity_sheet_name for c in "!'[]:*?/\\")
            or (
                activity_sheet_name != "Activities"
                and activity_sheet_name
                in (*TABLES, "Dashboard", "Analysis", *ZONE_CHART_TABS.values())
            )
        ):
            raise ValueError("Activity worksheet title is invalid or reserved")
        self.reporting_timezone = reporting_timezone
        self.activity_sheet_name = activity_sheet_name
        self.workbook = workbook
        self.credentials_path = credentials_path
        self._factory = service_factory
        self._service = None
        self._zone_columns = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="commuter-sheets"
        )
        self.deadline: float | None = None
        self._retry_not_before = 0.0

    async def _run(self, function, *args):
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor, function, *args
            )
        except (ValueError, SheetsError):
            raise
        except Exception as exc:
            raise SheetsError(
                "Google Sheets operation failed; check access/connectivity and retry"
            ) from exc

    def _sdk(self):
        if self._service is None:
            if self._factory is not None:
                self._service = self._factory()
            else:
                import google.auth
                import google_auth_httplib2
                import httplib2
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                credentials = (
                    service_account.Credentials.from_service_account_file(
                        str(self.credentials_path), scopes=scopes
                    )
                    if self.credentials_path
                    else google.auth.default(scopes=scopes)[0]
                )
                transport = google_auth_httplib2.AuthorizedHttp(
                    credentials, http=httplib2.Http(timeout=10)
                )
                self._service = build(
                    "sheets", "v4", http=transport, cache_discovery=False
                ).spreadsheets()
        return self._service

    def _execute(self, request):
        from googleapiclient.errors import HttpError

        for attempt in range(8):
            delay = max(0.0, self._retry_not_before - time.monotonic())
            if (
                self.deadline is not None
                and time.monotonic() + delay + 12 >= self.deadline
            ):
                raise SheetsError(
                    "Sheets retry deferred to the next run; export remains pending"
                )
            if delay:
                time.sleep(delay)
            if self.deadline is not None and time.monotonic() + 12 >= self.deadline:
                raise SheetsError(
                    "Sheets work deferred to stay within the run time budget"
                )
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                if exc.resp.status not in {429, 500, 502, 503, 504}:
                    raise SheetsError(
                        f"Google Sheets request failed (HTTP {exc.resp.status}); export remains pending"
                    ) from exc
                delay = max(
                    _retry_after_seconds(exc.resp),
                    min(2**attempt + random.random(), 64.0),
                )
                # All requests on this writer respect the cooldown, even if this
                # export is deferred or its retry limit is reached.
                self._retry_not_before = time.monotonic() + delay
                if attempt == 7:
                    raise SheetsError(
                        f"Google Sheets request failed (HTTP {exc.resp.status}); export remains pending"
                    ) from exc
                logger.warning(
                    "Google Sheets HTTP %s; backing off for %.1f seconds before retry %s of 7",
                    exc.resp.status,
                    delay,
                    attempt + 1,
                )

    def _title(self, title: str) -> str:
        return self.activity_sheet_name if title == "Activities" else title

    def _read(self, title: str) -> list[dict[str, object]]:
        return self._read_many((title,))[title]

    def _read_many(self, titles) -> dict[str, list[dict[str, object]]]:
        columns = {title: self._columns(title) for title in titles}
        response = self._execute(
            self._sdk()
            .values()
            .batchGet(
                spreadsheetId=self.workbook,
                ranges=[
                    f"'{self._title(title)}'!A:{column_name(len(columns[title]))}"
                    for title in titles
                ],
                valueRenderOption="UNFORMATTED_VALUE",
            )
        )
        result = {}
        if len(response.get("valueRanges", [])) != len(titles):
            raise ValueError("Sheets returned incomplete reporting tables")
        for title, table in zip(titles, response["valueRanges"]):
            values = table.get("values", [])
            if not values or tuple(values[0]) != columns[title]:
                raise ValueError(
                    f"Unexpected {title} schema; run sheets-setup on a compatible workbook"
                )
            result[title] = [
                dict(zip(columns[title], row + [""] * (len(columns[title]) - len(row))))
                for row in values[1:]
            ]
        return result

    def _range(self, title: str, row: int, values: list[object]) -> dict[str, object]:
        return {
            "range": f"'{self._title(title)}'!A{row}:{column_name(len(self._columns(title)))}{row}",
            "values": [["" if v is None else v for v in values]],
        }

    def _columns(self, title):
        if title in TABLES:
            return TABLES[title]
        if title not in self._zone_columns:
            values = self._execute(
                self._sdk()
                .values()
                .get(spreadsheetId=self.workbook, range=f"'{title}'!1:1")
            ).get("values", [])
            if not values or not values[0] or values[0][0] != "week_and_settings":
                raise ValueError("Zone chart schema is missing; run sheets-setup")
            self._zone_columns[title] = tuple(values[0])
        return self._zone_columns[title]

    def _write(self, ranges: list[dict[str, object]], option: str = "RAW") -> None:
        if ranges:
            metadata = self._execute(
                self._sdk().get(spreadsheetId=self.workbook, fields="sheets.properties")
            )
            properties = {
                s["properties"]["title"]: s["properties"] for s in metadata["sheets"]
            }
            required = {}
            for entry in ranges:
                title, cells = entry["range"].split("!")
                title = title.strip("'")
                row = max(int(v) for v in re.findall(r"\d+", cells))
                column = max(len(values) for values in entry["values"])
                previous = required.get(title, (0, 0))
                required[title] = max(row, previous[0]), max(column, previous[1])
            expansion = []
            for title, (rows, columns) in required.items():
                props = properties[title]
                grid = props["gridProperties"]
                if rows > grid["rowCount"] or columns > grid["columnCount"]:
                    expansion.append(
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": props["sheetId"],
                                    "gridProperties": {
                                        "rowCount": max(
                                            grid["rowCount"], ((rows + 99) // 100) * 100
                                        ),
                                        "columnCount": max(
                                            grid["columnCount"], columns
                                        ),
                                    },
                                },
                                "fields": "gridProperties.rowCount,gridProperties.columnCount",
                            }
                        }
                    )
            if expansion:
                self._execute(
                    self._sdk().batchUpdate(
                        spreadsheetId=self.workbook, body={"requests": expansion}
                    )
                )
            self._execute(
                self._sdk()
                .values()
                .batchUpdate(
                    spreadsheetId=self.workbook,
                    body={"valueInputOption": option, "data": ranges},
                )
            )

    def _index(self, rows, keys):
        index = {}
        for row_number, row in enumerate(rows, 2):
            if not row.get(keys[0]):
                continue
            if not isinstance(row[keys[0]], str):
                raise ValueError("Activity IDs must be stored as text")
            key = tuple(row[k] for k in keys)
            if key in index:
                raise ValueError(
                    "Duplicate reporting identity; repair the raw table before sync"
                )
            index[key] = row_number
        return index

    async def upsert(
        self, activity: dict[str, object], zones: list[dict[str, object]]
    ) -> None:
        await self._run(self._upsert, activity, zones)

    def _upsert(self, activity, zones):
        tables = self._read_many(("Activities", "Zone Time", "Strava HR Zones"))
        rows, existing_zones = tables["Activities"], tables["Zone Time"]
        activity_index = self._index(rows, ("activity_id",))
        self._index(existing_zones, ("activity_id", "zone"))
        position = activity_index.get((activity["activity_id"],), len(rows) + 2)
        updates = [
            self._range(
                "Activities", position, [activity.get(c) for c in ACTIVITY_COLUMNS]
            )
        ]
        obsolete = [
            i
            for i, row in enumerate(existing_zones, 2)
            if row.get("activity_id") == activity["activity_id"]
        ]
        spare = [
            i for i, row in enumerate(existing_zones, 2) if not row.get("activity_id")
        ]
        available = obsolete + spare
        for i, zone in enumerate(zones):
            position = (
                available[i]
                if i < len(available)
                else len(existing_zones) + 2 + i - len(available)
            )
            updates.append(
                self._range("Zone Time", position, [zone.get(c) for c in ZONE_COLUMNS])
            )
        for position in obsolete[len(zones) :]:
            updates.append(
                self._range("Zone Time", position, [None] * len(ZONE_COLUMNS))
            )
        reported = tables["Strava HR Zones"]
        self._index(reported, ("activity_id", "zone"))
        new = activity.get("reported_zones", [])
        locations = [
            i
            for i, r in enumerate(reported, 2)
            if r.get("activity_id") == activity["activity_id"]
        ]
        free = locations + [
            i for i, r in enumerate(reported, 2) if not r.get("activity_id")
        ]
        for i, row in enumerate(new):
            position = free[i] if i < len(free) else len(reported) + 2 + i - len(free)
            updates.append(
                self._range(
                    "Strava HR Zones",
                    position,
                    [row.get(c) for c in REPORTED_ZONE_COLUMNS],
                )
            )
        for position in locations[len(new) :]:
            updates.append(
                self._range(
                    "Strava HR Zones", position, [None] * len(REPORTED_ZONE_COLUMNS)
                )
            )
        self._write(updates)

    async def snapshot_settings(self):
        return await self._run(self._snapshot_settings)

    def _snapshot_settings(self):
        grouped = defaultdict(list)
        for row in self._read("Settings"):
            if row.get("sport"):
                grouped[
                    (str(row["sport"]), str(row["effective_from"]), str(row["version"]))
                ].append(row)
        zones = []
        dates = set()
        for (sport, date, version), rows in grouped.items():
            if (sport, date) in dates:
                raise ValueError(
                    "Multiple zone versions have the same sport/effective date"
                )
            dates.add((sport, date))
            metadata = ("confirmed", "max_gap_s", "method_source", "retrospective")
            if any(any(row[k] != rows[0][k] for k in metadata) for row in rows):
                raise ValueError("Zone metadata must agree within one settings version")

            def boolean(value):
                if value not in (True, False, "true", "false", "TRUE", "FALSE", ""):
                    raise ValueError("Zone confirmation must be true or false")
                return value is True or value in ("true", "TRUE")

            setting = ZoneSettings(
                sport,
                date,
                version,
                tuple(
                    ZoneBoundary(
                        str(r["zone"]),
                        float(r["lower_bpm"]),
                        float(r["upper_bpm"]) if r["upper_bpm"] != "" else None,
                    )
                    for r in rows
                ),
                boolean(rows[0]["confirmed"]),
                float(rows[0]["max_gap_s"]),
                str(rows[0]["method_source"]),
                boolean(rows[0]["retrospective"]),
            )
            validate_zones(setting)
            zones.append(setting)
        manual = self._read("Manual Inputs")
        self._index(manual, ("activity_id",))
        overrides = {}
        for row in manual:
            if not row["activity_id"]:
                continue
            category = str(row.get("category", ""))
            if category and category not in CATEGORIES:
                raise ValueError("Invalid manual category")
            overrides[row["activity_id"]] = {
                "category": category,
                "source": str(row.get("source", "")),
            }
        return tuple(zones), overrides

    async def finish(self, status: str, details: str = "") -> None:
        await self._run(self._finish, status, details)

    def _replace(self, title, records, old_count):
        return [
            self._range(title, i + 2, [record.get(c) for c in self._columns(title)])
            for i, record in enumerate(records)
        ] + [
            self._range(title, i + 2, [None] * len(self._columns(title)))
            for i in range(len(records), old_count)
        ]

    def _finish(self, status, details):
        tables = self._read_many(
            (
                "Activities",
                "Zone Time",
                "Weekly Summary",
                "Zone Summary",
                "Chart Data",
                "Daily Chart Data",
                "Sync Log",
                "Zone Pie Chart Data",
                "Strava HR Zones",
                *ZONE_CHART_TABS.values(),
            )
        )
        activities = [r for r in tables["Activities"] if r["activity_id"]]
        zones = [r for r in tables["Zone Time"] if r["activity_id"]]
        self._index(activities, ("activity_id",))
        self._index(zones, ("activity_id", "zone"))
        weeks, zone_summary = summarize(activities, zones)
        today = datetime.now(ZoneInfo(self.reporting_timezone)).date()
        weekly_charts, daily_charts = dashboard_data(activities, today)
        chart_start = weekly_charts[0]["week"]
        boundary_updates, new_catalog = self._boundary_ranges(
            zone_boundary_data(tables["Strava HR Zones"]), tables["Sync Log"]
        )
        self._write(
            self._replace("Weekly Summary", weeks, len(tables["Weekly Summary"]))
            + self._replace("Zone Summary", zone_summary, len(tables["Zone Summary"]))
            + self._replace("Chart Data", weekly_charts, len(tables["Chart Data"]))
            + self._replace(
                "Daily Chart Data", daily_charts, len(tables["Daily Chart Data"])
            )
            + self._replace(
                "Zone Pie Chart Data",
                zone_pie_data(activities, zones, today),
                len(tables["Zone Pie Chart Data"]),
            )
            + boundary_updates
        )
        if new_catalog:
            self._format_boundary_catalog()
        zone_updates = []
        for category, title in ZONE_CHART_TABS.items():
            columns = self._columns(title)
            chart_rows = {}
            for row in zone_summary:
                if (
                    row["category"]
                    not in (
                        {"virtual_bike", "bike_other"}
                        if category == "virtual_bike"
                        else {category}
                    )
                    or not chart_start <= row["week"] <= today.isoformat()
                ):
                    continue
                if row["zone"] not in columns:
                    raise ValueError(
                        "New zone chart columns require sheets-setup --refresh-charts"
                    )
                label = f"{row['week']} / {row['settings_version']}"
                entry = chart_rows.setdefault(label, {"week_and_settings": label})
                entry[row["zone"]] = entry.get(row["zone"], 0) + row["minutes"]
            zone_updates += self._replace(
                title,
                [chart_rows[k] for k in sorted(chart_rows)],
                len(tables[title]),
            )
        self._write(zone_updates)
        log = {
            "schema_version": "1",
            "status": status,
            "details": details,
            "last_attempt_utc": datetime.now(timezone.utc).isoformat(),
        }
        if status == "success":
            log["last_success_utc"] = log["last_attempt_utc"]
        existing = tables["Sync Log"]
        indices = {r["key"]: i for i, r in enumerate(existing, 2)}
        next_row = len(existing) + 2
        ranges = []
        for key, value in log.items():
            position = indices.get(key)
            if position is None:
                position, next_row = next_row, next_row + 1
            ranges.append(self._range("Sync Log", position, [key, value]))
        self._write(ranges)

    def _boundary_ranges(self, rows, log_rows):
        indices = {r["key"]: i for i, r in enumerate(log_rows, 2)}
        old = next(
            (r["value"] for r in log_rows if r["key"] == "zone_boundary_rows"), None
        )
        count = int(old) if old is not None else 0
        last_row = 8 + max(len(rows), count)
        existing = self._execute(
            self._sdk()
            .values()
            .get(
                spreadsheetId=self.workbook,
                range=f"'Analysis'!A7:E{last_row}",
                valueRenderOption="FORMULA",
            )
        ).get("values", [])
        for row_number, values in enumerate(existing, 7):
            if (old is None or row_number > 8 + count) and any(v != "" for v in values):
                raise ValueError(
                    "Analysis HR boundary table would overwrite user cells; clear its reserved A7:E range first"
                )
        updates = [
            {
                "range": "'Analysis'!A7:E8",
                "values": [
                    [
                        "Strava HR zone boundaries",
                        "",
                        "Blank upper = unbounded",
                        "",
                        "",
                    ],
                    list(BOUNDARY_HEADERS),
                ],
            }
        ]
        for i in range(max(len(rows), count)):
            values = (
                [
                    rows[i].get(k)
                    for k in (
                        "sport",
                        "zone",
                        "lower_bpm",
                        "upper_bpm",
                        "settings_version",
                    )
                ]
                if i < len(rows)
                else [None] * 5
            )
            updates.append(
                {
                    "range": f"'Analysis'!A{i + 9}:E{i + 9}",
                    "values": [["" if value is None else value for value in values]],
                }
            )
        position = indices.get("zone_boundary_rows", len(log_rows) + 2)
        updates.append(
            self._range("Sync Log", position, ["zone_boundary_rows", len(rows)])
        )
        if old is None:
            log_rows.append({"key": "zone_boundary_rows", "value": len(rows)})
        else:
            log_rows[position - 2]["value"] = len(rows)
        return updates, old is None

    def _format_boundary_catalog(self):
        sdk = self._sdk()
        metadata = self._execute(
            sdk.get(spreadsheetId=self.workbook, fields="sheets.properties")
        )
        sheet_id = next(
            s["properties"]["sheetId"]
            for s in metadata["sheets"]
            if s["properties"]["title"] == "Analysis"
        )
        self._execute(
            sdk.batchUpdate(
                spreadsheetId=self.workbook,
                body={
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 7,
                                    "endRowIndex": 8,
                                    "endColumnIndex": 5,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": {
                                            "red": 0.1,
                                            "green": 0.2,
                                            "blue": 0.35,
                                        },
                                        "textFormat": {
                                            "bold": True,
                                            "foregroundColor": {
                                                "red": 1,
                                                "green": 1,
                                                "blue": 1,
                                            },
                                        },
                                    }
                                },
                                "fields": "userEnteredFormat",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 4,
                                    "endIndex": 5,
                                },
                                "properties": {"pixelSize": 260},
                                "fields": "pixelSize",
                            }
                        },
                    ]
                },
            )
        )

    async def reconcile(
        self,
        present: set[str],
        after: int,
        before: int,
        *,
        confirmed_removal: str | None = None,
    ) -> None:
        await self._run(self._reconcile, present, after, before, confirmed_removal)

    def _reconcile(self, present, after, before, confirmed_removal):
        rows = self._read("Activities")
        self._index(rows, ("activity_id",))
        changes = []
        for i, row in enumerate(rows, 2):
            if not row["activity_id"] or row.get("status") == "removed":
                continue
            moment = datetime.fromisoformat(str(row["start_utc"])).timestamp()
            if confirmed_removal == row["activity_id"]:
                row["status"] = "removed"
            elif after <= moment < before:
                row["status"] = (
                    "active"
                    if row["activity_id"] in present
                    else "unavailable_pending_review"
                )
            else:
                continue
            changes.append(
                self._range("Activities", i, [row.get(c) for c in ACTIVITY_COLUMNS])
            )
        self._write(changes)

    def _refresh_zone_status(self):
        sdk = self._sdk()
        previous = self._execute(
            sdk.values().get(
                spreadsheetId=self.workbook,
                range="'Analysis'!B6",
                valueRenderOption="FORMULA",
            )
        ).get("values", [])
        legacy = '=IF(COUNTA(Settings!A2:A)=0,"Awaiting boundaries; measurements exported","See Settings; only confirmed zones publish")'
        if (
            previous
            and previous[0]
            and previous[0][0] in {legacy, legacy.replace("Settings!", "'Settings'!")}
        ):
            self._write(
                [{"range": "'Analysis'!B6", "values": [[self._zone_status_formula()]]}],
                "USER_ENTERED",
            )

    def _zone_status_formula(self):
        return (
            f"=IF(COUNTIF('{self.activity_sheet_name}'!AF2:AF,\"strava_reported\")>0,"
            '"Strava-reported zones; personal method unreviewed",'
            'IF(COUNTA(\'Settings\'!A2:A)=0,"Awaiting zone data","See Settings; confirmed custom zones"))'
        )

    async def setup(self, refresh_charts: bool = False) -> None:
        await self._run(self._setup, refresh_charts)

    def _setup(self, refresh_charts=False):
        sdk = self._sdk()
        metadata = self._execute(
            sdk.get(spreadsheetId=self.workbook, fields="sheets(properties,charts)")
        )
        sheets = {s["properties"]["title"]: s for s in metadata.get("sheets", [])}
        titles = (
            "Dashboard",
            "Analysis",
            *(self._title(t) for t in TABLES),
            *ZONE_CHART_TABS.values(),
        )
        missing = [t for t in titles if t not in sheets]
        if missing:
            self._execute(
                sdk.batchUpdate(
                    spreadsheetId=self.workbook,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": t}}} for t in missing
                        ]
                    },
                )
            )
            metadata = self._execute(
                sdk.get(spreadsheetId=self.workbook, fields="sheets(properties,charts)")
            )
            sheets = {s["properties"]["title"]: s for s in metadata["sheets"]}
        ranges = []
        formatting = []
        for title, columns in TABLES.items():
            properties = sheets[self._title(title)]["properties"]
            if properties.get("gridProperties", {}).get("columnCount", 26) < len(
                columns
            ):
                self._execute(
                    sdk.batchUpdate(
                        spreadsheetId=self.workbook,
                        body={
                            "requests": [
                                {
                                    "updateSheetProperties": {
                                        "properties": {
                                            "sheetId": properties["sheetId"],
                                            "gridProperties": {
                                                "columnCount": len(columns)
                                            },
                                        },
                                        "fields": "gridProperties.columnCount",
                                    }
                                }
                            ]
                        },
                    )
                )
            existing = self._execute(
                sdk.values().get(
                    spreadsheetId=self.workbook,
                    range=f"'{self._title(title)}'!A1:{column_name(len(columns))}1",
                )
            ).get("values", [])
            if (
                title == "Chart Data"
                and existing
                and tuple(existing[0]) in (columns[:6], columns[:7])
            ):
                ranges.append(self._range(title, 1, list(columns)))
                existing = [list(columns)]
            if existing and tuple(existing[0]) != columns:
                raise ValueError(
                    f"Existing {title} has incompatible columns; use an empty workbook"
                )
            if not existing:
                ranges.append(self._range(title, 1, list(columns)))
                sheet_id = sheets[self._title(title)]["properties"]["sheetId"]
                formatting.extend(
                    [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {"frozenRowCount": 1},
                                },
                                "fields": "gridProperties.frozenRowCount",
                            }
                        },
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endRowIndex": 1,
                                    "endColumnIndex": len(columns),
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": {
                                            "red": 0.1,
                                            "green": 0.2,
                                            "blue": 0.35,
                                        },
                                        "textFormat": {
                                            "bold": True,
                                            "foregroundColor": {
                                                "red": 1,
                                                "green": 1,
                                                "blue": 1,
                                            },
                                        },
                                        "wrapStrategy": "WRAP",
                                    }
                                },
                                "fields": "userEnteredFormat",
                            }
                        },
                    ]
                )
        self._write(ranges)
        if formatting:
            self._execute(
                sdk.batchUpdate(
                    spreadsheetId=self.workbook, body={"requests": formatting}
                )
            )
        settings, _ = self._snapshot_settings()
        zone_names = sorted(
            {b.name for z in settings for b in z.boundaries}
            | {f"Z{i}" for i in range(1, 6)}
        )
        for title in ZONE_CHART_TABS.values():
            old = self._execute(
                sdk.values().get(spreadsheetId=self.workbook, range=f"'{title}'!1:1")
            ).get("values", [])
            if old and old[0] and old[0][0] != "week_and_settings":
                raise ValueError("Incompatible zone chart table")
            columns = tuple(
                dict.fromkeys(
                    (
                        "week_and_settings",
                        *(old[0][1:] if old else []),
                        *zone_names,
                        "unknown",
                    )
                )
            )
            self._zone_columns[title] = columns
            properties = sheets[title]["properties"]
            self._execute(
                sdk.batchUpdate(
                    spreadsheetId=self.workbook,
                    body={
                        "requests": [
                            {
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": properties["sheetId"],
                                        "hidden": True,
                                        "gridProperties": {
                                            "columnCount": max(
                                                len(columns),
                                                properties.get(
                                                    "gridProperties", {}
                                                ).get("columnCount", 26),
                                            )
                                        },
                                    },
                                    "fields": "hidden,gridProperties.columnCount",
                                }
                            }
                        ]
                    },
                )
            )
            self._write([self._range(title, 1, list(columns))])
        analysis = self._execute(
            sdk.values().get(spreadsheetId=self.workbook, range="'Analysis'!A1")
        ).get("values", [])
        if not analysis:
            self._write(
                [
                    {
                        "range": "'Analysis'!A1:B6",
                        "values": [
                            ["Training analysis", "Value"],
                            [
                                "Running miles",
                                f"=SUM('{self.activity_sheet_name}'!P2:P)",
                            ],
                            [
                                "Moving minutes",
                                f"=SUM('{self.activity_sheet_name}'!Q2:Q)/60",
                            ],
                            [
                                "Classified HR minutes",
                                f"=SUM('{self.activity_sheet_name}'!AB2:AB)/60",
                            ],
                            [
                                "Last successful sync",
                                '=IFNA(VLOOKUP("last_success_utc",\'Sync Log\'!A:B,2,FALSE),"")',
                            ],
                            [
                                "HR zones",
                                self._zone_status_formula(),
                            ],
                        ],
                    }
                ],
                "USER_ENTERED",
            )
        self._refresh_zone_status()
        dashboard = sheets["Dashboard"]
        if refresh_charts:
            import json

            log = {r["key"]: r["value"] for r in self._read("Sync Log")}
            owned = json.loads(log.get("managed_chart_ids", "[]"))
            deletes = [
                {"deleteEmbeddedObject": {"objectId": i}}
                for i in owned
                if any(c["chartId"] == i for c in dashboard.get("charts", []))
            ]
            if deletes:
                self._execute(
                    sdk.batchUpdate(
                        spreadsheetId=self.workbook, body={"requests": deletes}
                    )
                )
        if refresh_charts or not dashboard.get("charts"):
            summary_id = sheets["Chart Data"]["properties"]["sheetId"]
            self._execute(
                sdk.batchUpdate(
                    spreadsheetId=self.workbook,
                    body={
                        "requests": [
                            {
                                "repeatCell": {
                                    "range": {
                                        "sheetId": summary_id,
                                        "startRowIndex": 1,
                                        "startColumnIndex": start_column,
                                        "endColumnIndex": end_column,
                                    },
                                    "cell": {
                                        "userEnteredFormat": {
                                            "numberFormat": {
                                                "type": "NUMBER",
                                                "pattern": "0.0",
                                            }
                                        }
                                    },
                                    "fields": "userEnteredFormat.numberFormat",
                                }
                            }
                            for start_column, end_column in ((1, 4), (6, 7), (7, 9))
                        ]
                    },
                )
            )
            charts = []
            for i, (title, sheet_id, columns, chart_type) in enumerate(
                [
                    (
                        "Weekly duration by category (minutes; last 12 weeks)",
                        summary_id,
                        [1, 2, 3, 7, 8],
                        "COLUMN",
                    ),
                    ("Weekly running miles (last 12 weeks)", summary_id, [4], "LINE"),
                    (
                        "Weekly non-commute minutes (last 12 weeks)",
                        summary_id,
                        [6],
                        "COLUMN",
                    ),
                    (
                        "Rolling 7-day running duration (minutes; last 12 weeks)",
                        sheets["Daily Chart Data"]["properties"]["sheetId"],
                        [1],
                        "LINE",
                    ),
                    (
                        "Rolling 7-day biking duration, non-commute (minutes; last 12 weeks)",
                        sheets["Daily Chart Data"]["properties"]["sheetId"],
                        [2],
                        "LINE",
                    ),
                    *[
                        (
                            f"Weekly {'non-commute biking' if category == 'virtual_bike' else category} HR zone minutes (last 12 weeks; unknown shown)",
                            sheets[title]["properties"]["sheetId"],
                            list(range(1, len(self._columns(title)))),
                            "COLUMN",
                        )
                        for category, title in ZONE_CHART_TABS.items()
                        if category != "bike_commute"
                    ],
                ]
            ):

                def source(col):
                    return {
                        "sources": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "startColumnIndex": col,
                                "endColumnIndex": col + 1,
                            }
                        ]
                    }

                spec = {
                    "title": title,
                    "basicChart": {
                        "chartType": chart_type,
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "domains": [{"domain": {"sourceRange": source(0)}}],
                        "series": [
                            {
                                "series": {"sourceRange": source(c)},
                                "targetAxis": "LEFT_AXIS",
                            }
                            for c in columns
                        ],
                    },
                }
                if chart_type == "COLUMN":
                    spec["basicChart"]["stackedType"] = "STACKED"
                if title == "Weekly non-commute minutes (last 12 weeks)":
                    spec["basicChart"]["series"][0]["dataLabel"] = {"type": "DATA"}
                    spec["subtitle"] = (
                        "Training duration excludes bike commutes and hikes."
                    )
                if title == "Weekly duration by category (minutes; last 12 weeks)":
                    spec["basicChart"]["totalDataLabel"] = {"type": "DATA"}
                charts.append(
                    {
                        "addChart": {
                            "chart": {
                                "spec": spec,
                                "position": {
                                    "overlayPosition": {
                                        "anchorCell": {
                                            "sheetId": dashboard["properties"][
                                                "sheetId"
                                            ],
                                            "rowIndex": i * 20,
                                            "columnIndex": 0,
                                        },
                                        "widthPixels": 900,
                                        "heightPixels": 350,
                                    }
                                },
                            }
                        }
                    }
                )
            response = self._execute(
                sdk.batchUpdate(spreadsheetId=self.workbook, body={"requests": charts})
            )
            import json

            chart_ids = [
                r["addChart"]["chart"]["chartId"]
                for r in response.get("replies", [])
                if "addChart" in r
            ]
            existing = self._read("Sync Log")
            position = next(
                (
                    i
                    for i, r in enumerate(existing, 2)
                    if r["key"] == "managed_chart_ids"
                ),
                len(existing) + 2,
            )
            self._write(
                [
                    self._range(
                        "Sync Log",
                        position,
                        ["managed_chart_ids", json.dumps(chart_ids)],
                    )
                ]
            )

        self._add_zone_pies(refresh_charts)

    def _add_zone_pies(self, refresh=False):
        sdk = self._sdk()
        metadata = self._execute(
            sdk.get(spreadsheetId=self.workbook, fields="sheets(properties,charts)")
        )
        sheets = {s["properties"]["title"]: s for s in metadata["sheets"]}
        dashboard = sheets["Dashboard"]
        charts = dashboard.get("charts", [])
        log_rows = self._read("Sync Log")
        log = {r["key"]: r["value"] for r in log_rows}
        registered = {} if refresh else json.loads(log.get("zone_pie_chart_ids", "{}"))
        owned = json.loads(log.get("managed_chart_ids", "[]"))
        overlays = [c.get("position", {}).get("overlayPosition", {}) for c in charts]
        next_row = (
            max(
                (
                    p.get("anchorCell", {}).get("rowIndex", 0)
                    + math.ceil(p.get("heightPixels", 350) / 20)
                    for p in overlays
                ),
                default=0,
            )
            + 3
        )
        requests, kinds = [], []
        sheet_id = sheets["Zone Pie Chart Data"]["properties"]["sheetId"]
        for column, (kind, label) in enumerate(
            (("run", "Running"), ("bike", "Biking"), ("combined", "Combined")), 1
        ):
            if kind in registered:
                # A logged ID missing from the workbook represents an owner deletion.
                continue
            title = f"{label} HR zones (last 7 days; no commutes)"
            existing = next(
                (
                    c
                    for c in charts
                    if c["spec"].get("title") == title and "pieChart" in c["spec"]
                ),
                None,
            )
            if existing:
                registered[kind] = existing["chartId"]
                continue

            def source(col):
                return {
                    "sourceRange": {
                        "sources": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": col,
                                "endColumnIndex": col + 1,
                            }
                        ]
                    }
                }

            subtitle = (
                "Sport-specific boundaries; unknown/unassigned elapsed time included"
                if kind == "combined"
                else "Unknown/unassigned elapsed time included"
            )
            requests.append(
                {
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": title,
                                "subtitle": subtitle,
                                "fontName": "Arial",
                                "pieChart": {
                                    "legendPosition": "RIGHT_LEGEND",
                                    "domain": source(0),
                                    "series": source(column),
                                    "threeDimensional": False,
                                },
                            },
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": dashboard["properties"]["sheetId"],
                                        "rowIndex": next_row,
                                        "columnIndex": 0,
                                    },
                                    "widthPixels": 900,
                                    "heightPixels": 350,
                                }
                            },
                        }
                    }
                }
            )
            kinds.append(kind)
            next_row += 20
        if requests:
            response = self._execute(
                sdk.batchUpdate(
                    spreadsheetId=self.workbook, body={"requests": requests}
                )
            )
            for kind, reply in zip(kinds, response["replies"]):
                registered[kind] = reply["addChart"]["chart"]["chartId"]
        indices = {r["key"]: i for i, r in enumerate(log_rows, 2)}
        updates = []
        next_log_row = len(log_rows) + 2
        for key, value in (
            ("zone_pie_chart_ids", json.dumps(registered)),
            (
                "managed_chart_ids",
                json.dumps(sorted(set(owned) | set(registered.values()))),
            ),
        ):
            position = indices.get(key)
            if position is None:
                position = next_log_row
                next_log_row += 1
            updates.append(self._range("Sync Log", position, [key, value]))
        self._write(updates)

    async def close(self) -> None:
        def close_transport():
            if self._service is not None and hasattr(self._service, "close"):
                self._service.close()

        try:
            await self._run(close_transport)
        finally:
            self._executor.shutdown(wait=True)
