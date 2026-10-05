"""Training projection with trailing three-second HR and pace measurements."""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from commuter.models import (
    ActivityEnvelope,
    RollingMeasurement,
    ZoneResult,
    ZoneSettings,
)
from commuter.pipeline import ProcessingContext
from commuter.state import content_version

METHOD_VERSION = "hr-pace-available-trailing-3s-v2"
SCHEMA_VERSION = "1"
WINDOW_S = 3.0
CATEGORIES = {"run", "virtual_bike", "bike_commute", "bike_other", "other"}
CYCLING = {
    "Ride",
    "MountainBikeRide",
    "GravelRide",
    "EBikeRide",
    "EMountainBikeRide",
    "Handcycle",
    "Velomobile",
}


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def workout_pace(distance_m: object, seconds: object) -> float | None:
    distance, duration = number(distance_m), number(seconds)
    if distance is None or duration is None or distance <= 0 or duration <= 0:
        return None
    return 1000 * duration / distance


def recording_gap_cutoff(times) -> float:
    """Trust ordinary cadence; gaps over three median intervals remain unknown."""
    intervals = [
        b - a
        for a, b in zip(times, times[1:])
        if number(a) is not None and number(b) is not None and b > a
    ]
    return max(WINDOW_S, 3 * median(intervals)) if intervals else WINDOW_S


def rolling_measurements(
    times, heart_rate, distance, moving, max_gap_s: float
) -> list[RollingMeasurement]:
    """Integrate up to three available trailing seconds, resetting after gaps.

    HR samples hold until the next timestamp. Distance increments are linear
    within trusted intervals. A window ending at t applies to [t, next_t).
    Pace requires every interval in its window to be recorded as moving.
    """

    if number(max_gap_s) is None or max_gap_s <= 0:
        raise ValueError("A positive recording-gap cutoff is required")
    if any(number(t) is None or t < 0 for t in times) or any(
        b <= a for a, b in zip(times, times[1:])
    ):
        raise ValueError(
            "Stream timestamps must be finite, nonnegative and strictly increasing"
        )
    size = len(times)
    if any(
        stream is not None and len(stream) != size
        for stream in (heart_rate, distance, moving)
    ):
        raise ValueError("Stream lengths do not align")
    hr_area, distance_area = [0.0], [0.0]
    hr_invalid, pace_invalid = [0], [0]
    for i in range(max(0, size - 1)):
        dt = times[i + 1] - times[i]
        hr = number(heart_rate[i]) if heart_rate is not None else None
        start_d = number(distance[i]) if distance is not None else None
        end_d = number(distance[i + 1]) if distance is not None else None
        hr_ok = dt <= max_gap_s and hr is not None and hr > 0
        pace_ok = (
            dt <= max_gap_s
            and start_d is not None
            and end_d is not None
            and end_d >= start_d >= 0
            and moving is not None
            and moving[i] is True
        )
        hr_area.append(hr_area[-1] + (hr * dt if hr_ok else 0))
        distance_area.append(distance_area[-1] + (end_d - start_d if pace_ok else 0))
        hr_invalid.append(hr_invalid[-1] + (not hr_ok))
        pace_invalid.append(pace_invalid[-1] + (not pace_ok))
    result = []
    hr_start = pace_start = 0
    for i, timestamp in enumerate(times):
        if i and hr_invalid[i] != hr_invalid[i - 1]:
            hr_start = i
        if i and pace_invalid[i] != pace_invalid[i - 1]:
            pace_start = i
        hr_mean = pace = None
        left = max(timestamp - WINDOW_S, times[hr_start])
        duration = timestamp - left
        if duration > 0:
            j = bisect_right(times, left) - 1
            cut = left - times[j]
            hr_mean = (hr_area[i] - hr_area[j] - cut * number(heart_rate[j])) / duration
        left = max(timestamp - WINDOW_S, times[pace_start])
        duration = timestamp - left
        if duration > 0:
            j = bisect_right(times, left) - 1
            cut = left - times[j]
            speed = (distance[j + 1] - distance[j]) / (times[j + 1] - times[j])
            travelled = distance_area[i] - distance_area[j] - cut * speed
            if travelled > 0:
                pace = duration * 1000 / travelled
        dt = times[i + 1] - timestamp if i + 1 < size else 0
        trusted = dt <= max_gap_s
        increment = None
        if (
            trusted
            and dt > 0
            and distance is not None
            and moving is not None
            and moving[i] is True
        ):
            a, b = number(distance[i]), number(distance[i + 1])
            if a is not None and b is not None and b >= a >= 0:
                increment = b - a
        result.append(
            RollingMeasurement(
                timestamp,
                hr_mean if trusted else None,
                pace if trusted else None,
                dt,
                increment,
                moving[i] if moving is not None else None,
            )
        )
    return result


def validate_zones(settings: ZoneSettings) -> None:
    if (
        settings.sport not in {"run", "cycling"}
        or not settings.version
        or not settings.method_source
    ):
        raise ValueError("Zone sport, version and method source are required")
    datetime.strptime(settings.effective_from, "%Y-%m-%d")
    if (
        number(settings.max_gap_s) is None
        or settings.max_gap_s <= 0
        or not settings.boundaries
    ):
        raise ValueError("Zone settings need a positive gap cutoff and boundaries")
    names = [z.name for z in settings.boundaries]
    if len(names) != len(set(names)) or not all(names) or "unknown" in names:
        raise ValueError("Zone names must be unique and cannot be unknown")
    for i, zone in enumerate(settings.boundaries):
        if (
            number(zone.lower_bpm) is None
            or zone.lower_bpm < 0
            or (
                zone.upper_bpm is not None
                and (number(zone.upper_bpm) is None or zone.upper_bpm <= zone.lower_bpm)
            )
        ):
            raise ValueError("Invalid zone boundaries")
        if i and (
            settings.boundaries[i - 1].upper_bpm is None
            or settings.boundaries[i - 1].upper_bpm != zone.lower_bpm
        ):
            raise ValueError("Zone boundaries must be ordered and contiguous")


def calculate_zones(
    samples: list[RollingMeasurement], settings: ZoneSettings
) -> ZoneResult:
    validate_zones(settings)
    seconds = {z.name: 0.0 for z in settings.boundaries}
    pace_time = dict(seconds)
    pace_distance = dict(seconds)
    unknown = 0.0
    for sample in samples:
        zone = next(
            (
                z.name
                for z in settings.boundaries
                if sample.heart_rate_bpm is not None
                and z.lower_bpm <= sample.heart_rate_bpm
                and (z.upper_bpm is None or sample.heart_rate_bpm < z.upper_bpm)
            ),
            None,
        )
        if zone is None:
            unknown += sample.interval_s
        else:
            seconds[zone] += sample.interval_s
            if (
                sample.pace_s_km is not None
                and sample.distance_m is not None
                and sample.distance_m > 0
            ):
                # Preserve the approved rolling pace's time/distance basis.
                pace_time[zone] += sample.interval_s
                pace_distance[zone] += sample.interval_s * 1000 / sample.pace_s_km
    return ZoneResult(seconds, sum(seconds.values()), unknown, pace_time, pace_distance)


def stream_data(streams: dict[str, object], key: str):
    value = streams.get(key)
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, list):
        raise ValueError(f"Invalid {key} stream data")
    return data


class TrainingSheetsProcessor:
    """Replace training outputs and request their export through the Sheets adapter."""

    name = "sheets"

    async def process(
        self, context: ProcessingContext, activity: ActivityEnvelope
    ) -> ActivityEnvelope:
        source = activity.source
        sport = source.get("sport_type") or source.get("type")
        category = (
            "run"
            if sport in {"Run", "TrailRun"}
            else "virtual_bike"
            if sport == "VirtualRide"
            else "bike_commute"
            if sport in CYCLING
            and (activity.effective_commute or source.get("commute") is True)
            else "bike_other"
            if sport in CYCLING
            else "other"
        )
        override = context.overrides.get(str(activity.id), {})
        category = override.get("category") or category
        if category not in CATEGORIES:
            raise ValueError("Invalid manual activity category")
        start = source.get("start_date")
        if not isinstance(start, str):
            raise ValueError(f"Activity {activity.id} has no UTC start date")
        moment = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("Activity start date has no timezone")
        local = moment.astimezone(ZoneInfo(context.reporting_timezone))
        date = local.date().isoformat()
        week = (local.date() - timedelta(days=local.weekday())).isoformat()
        distance, moving_s, elapsed_s = (
            number(source.get("distance")),
            number(source.get("moving_time")),
            number(source.get("elapsed_time")),
        )
        distance = distance if distance is not None and distance >= 0 else None
        moving_s = moving_s if moving_s is not None and moving_s >= 0 else None
        elapsed_s = elapsed_s if elapsed_s is not None and elapsed_s >= 0 else None
        row = {
            "activity_id": str(activity.id),
            "name": source.get("name", ""),
            "strava_url": f"https://www.strava.com/activities/{activity.id}",
            "sport_type": sport,
            "start_utc": moment.astimezone(timezone.utc).isoformat(),
            "start_local": source.get("start_date_local"),
            "timezone": source.get("timezone"),
            "activity_date": date,
            "week": week,
            "category": category,
            "source": override.get("source") or source.get("device_name"),
            "commute": source.get("commute") is True,
            "effective_commute": activity.effective_commute
            or source.get("commute") is True,
            "trainer": source.get("trainer"),
            "distance_m": distance,
            "running_miles": distance / 1609.344
            if category == "run" and distance is not None
            else None,
            "moving_s": moving_s,
            "elapsed_s": elapsed_s,
            "average_hr_bpm": number(source.get("average_heartrate")),
            "max_hr_bpm": number(source.get("max_heartrate")),
            "moving_pace_s_km": workout_pace(distance, moving_s)
            if category == "run"
            else None,
            "elapsed_pace_s_km": workout_pace(distance, elapsed_s)
            if category == "run"
            else None,
            "method_version": METHOD_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_version": activity.source_version,
            "settings_version": "",
            "zone_availability": "missing_hr",
            "stream_span_s": None,
            "classified_s": None,
            "unknown_s": None,
            "hr_coverage": None,
            "workout_hr_coverage": None,
            "rolling_hr_mean_bpm": None,
            "rolling_pace_s_km": None,
            "rolling_window_s": WINDOW_S,
            "duration_basis": "recorded_elapsed_stream",
            "status": "active",
            "retrospective": False,
        }
        zone_rows = []
        streams = activity.streams or {}
        hr, times = stream_data(streams, "heartrate"), stream_data(streams, "time")
        settings_sport = (
            "run"
            if sport in {"Run", "TrailRun"}
            else "cycling"
            if sport in CYCLING or sport == "VirtualRide"
            else "other"
        )
        candidates = [
            z
            for z in context.zones
            if z.sport == settings_sport and z.effective_from <= date
        ]
        settings = (
            max(candidates, key=lambda z: z.effective_from) if candidates else None
        )
        if hr:
            row["zone_availability"] = (
                "missing_stream"
                if not times
                else "missing_thresholds"
                if settings is None
                else "unconfirmed_settings"
                if not settings.confirmed
                else "available"
            )
        if settings:
            validate_zones(settings)
            row["settings_version"] = settings.version
            row["retrospective"] = settings.retrospective
        gap = settings.max_gap_s if settings else context.max_gap_s
        if gap is None:
            gap = recording_gap_cutoff(times or [])
        row["recording_gap_cutoff_s"] = gap
        samples = []
        if times and gap is not None:
            samples = rolling_measurements(
                times,
                hr,
                stream_data(streams, "distance"),
                stream_data(streams, "moving"),
                gap,
            )
            valid_hr_time = sum(
                s.interval_s for s in samples if s.heart_rate_bpm is not None
            )
            row["rolling_hr_mean_bpm"] = (
                sum(
                    s.heart_rate_bpm * s.interval_s
                    for s in samples
                    if s.heart_rate_bpm is not None
                )
                / valid_hr_time
                if valid_hr_time
                else None
            )
            pace_time = sum(s.interval_s for s in samples if s.pace_s_km is not None)
            pace_distance = sum(
                s.interval_s * 1000 / s.pace_s_km
                for s in samples
                if s.pace_s_km is not None
            )
            row["rolling_pace_s_km"] = (
                pace_time * 1000 / pace_distance
                if pace_distance and category == "run"
                else None
            )
            row["rolling_pace_time_s"] = pace_time
            row["rolling_pace_distance_m"] = pace_distance
            row["stream_span_s"] = times[-1] - times[0]
        row["rolling_availability"] = (
            "unconfigured_gap_rule"
            if gap is None
            else "missing_stream"
            if not times
            else "available"
            if any(
                s.interval_s > 0
                and (s.heart_rate_bpm is not None or s.pace_s_km is not None)
                for s in samples
            )
            else "insufficient_trusted_coverage"
        )
        if times and hr and settings and settings.confirmed:
            zones = calculate_zones(samples, settings)
            span = times[-1] - times[0]
            row.update(
                stream_span_s=span,
                classified_s=zones.classified_s,
                unknown_s=zones.unknown_s,
                hr_coverage=zones.classified_s / span if span else None,
                workout_hr_coverage=zones.classified_s / elapsed_s
                if elapsed_s
                else None,
            )
            for name, seconds in {**zones.seconds, "unknown": zones.unknown_s}.items():
                zone_rows.append(
                    {
                        "activity_id": str(activity.id),
                        "week": week,
                        "category": category,
                        "zone": name,
                        "seconds": seconds,
                        "settings_version": settings.version,
                        "method_version": METHOD_VERSION,
                        "pace_time_s": zones.pace_time_s.get(name),
                        "pace_distance_m": zones.pace_distance_m.get(name),
                        "availability": "available",
                    }
                )
        reported_rows = []
        native = next(
            (z for z in activity.reported_zones if z.get("type") == "heartrate"), None
        )
        if native and settings_sport in {"run", "cycling"}:
            buckets = native.get("distribution_buckets", [])
            boundaries = [(b.get("min"), b.get("max")) for b in buckets]
            native_version = (
                f"strava-{settings_sport}-{content_version(boundaries)[:12]}"
            )
            for index, bucket in enumerate(buckets, 1):
                seconds, lower, upper = (
                    number(bucket.get("time")),
                    number(bucket.get("min")),
                    number(bucket.get("max")),
                )
                if seconds is None or seconds < 0 or lower is None or upper is None:
                    raise ValueError("Invalid Strava heart-rate zone bucket")
                reported_rows.append(
                    {
                        "activity_id": str(activity.id),
                        "activity_date": date,
                        "week": week,
                        "category": category,
                        "sport": settings_sport,
                        "zone": f"Z{index}",
                        "lower_bpm": lower,
                        "upper_bpm": None if upper == -1 else upper,
                        "seconds": seconds,
                        "settings_version": native_version,
                        "method_version": "strava-reported-v1",
                        "sensor_based": native.get("sensor_based"),
                        "availability": "strava_reported",
                    }
                )
            if not (settings and settings.confirmed):
                classified = sum(z["seconds"] for z in reported_rows)
                unknown = (
                    max(0, elapsed_s - classified) if elapsed_s is not None else None
                )
                row.update(
                    zone_availability="strava_reported",
                    settings_version=native_version,
                    classified_s=classified,
                    unknown_s=unknown,
                    hr_coverage=None,
                    workout_hr_coverage=classified / elapsed_s if elapsed_s else None,
                    duration_basis="strava_reported_zones;unassigned_elapsed",
                    retrospective=False,
                )
                zone_rows = [
                    z | {"pace_time_s": None, "pace_distance_m": None}
                    for z in reported_rows
                ]
                if unknown is not None:
                    zone_rows.append(
                        {
                            "activity_id": str(activity.id),
                            "week": week,
                            "category": category,
                            "zone": "unknown",
                            "seconds": unknown,
                            "settings_version": native_version,
                            "method_version": "strava-reported-v1",
                            "availability": "unassigned_elapsed",
                        }
                    )
        row["reported_zones"] = reported_rows
        row["zones"] = zone_rows
        row["settings_snapshot"] = content_version(
            {
                "zones": [
                    vars(z) | {"boundaries": [vars(b) for b in z.boundaries]}
                    for z in context.zones
                ],
                "overrides": context.overrides,
                "timezone": context.reporting_timezone,
                "max_gap_s": context.max_gap_s,
                "method_version": METHOD_VERSION,
            }
        )
        row["moving_pace_s_mile"] = (
            row["moving_pace_s_km"] * 1.609344
            if row["moving_pace_s_km"] is not None
            else None
        )
        activity.training = row
        if not context.dry_run:
            if context.sheets is None:
                raise ValueError("Training processor requires a Sheets adapter")
            await context.sheets.upsert(row, zone_rows)
        return activity
