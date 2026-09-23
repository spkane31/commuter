"""Commute matching, savings calculation, and scheduled Strava synchronization."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol

from commuter.models import GASOLINE_CO2_GRAMS_PER_GALLON, ActivityProcessing, CommuteConfiguration, Coordinate, Location
from commuter.store import CredentialStore

EARTH_RADIUS_M = 6_371_000
METERS_PER_MILE = Decimal("1609.344")
MANAGED_BLOCK_PATTERN = re.compile(r"--- Commuter ---\n.*?\n--- /Commuter ---", re.DOTALL)
logger = logging.getLogger(__name__)


class CommuteConfigurationError(ValueError):
    """Raised when a local commute rule cannot produce a reliable update."""


class ActivityClient(Protocol):
    """The Strava activity operations used by the local poller."""

    async def list_athlete_activities(
        self,
        access_token: str,
        *,
        after: int,
        per_page: int = 100,
    ) -> list[dict[str, object]]: ...

    async def get_activity(self, access_token: str, activity_id: int) -> dict[str, object]: ...

    async def update_activity(self, access_token: str, activity_id: int, update: dict[str, object]) -> None: ...


class TokenProvider(Protocol):
    """The small token lifecycle interface used by the local poller."""

    async def get_access_token(self, athlete_id: int) -> str: ...


class ActivityNotifier(Protocol):
    """The notification operation invoked after a successful Strava update."""

    async def activity_updated(
        self,
        *,
        activity_id: int,
        estimated_savings: str,
        cumulative_savings: str,
        co2_avoided: str,
        cumulative_co2_avoided: str,
    ) -> None: ...


@dataclass(frozen=True)
class ActivityDetails:
    """The subset of a detailed Strava activity needed for a commute decision."""

    id: int
    sport_type: str | None
    start: Coordinate | None
    end: Coordinate | None
    description: str | None
    commute: bool
    activity_date: str | None
    distance_m: float | None


@dataclass(frozen=True)
class CommuteDecision:
    """The result and human-readable reason for evaluating one activity."""

    matches: bool
    reason: str


@dataclass
class SyncResult:
    """Non-sensitive result summary for one polling run."""

    updated_activity_ids: list[int] = field(default_factory=list)
    would_update_activity_ids: list[int] = field(default_factory=list)
    non_matching_activity_ids: list[int] = field(default_factory=list)
    unconfigured_athlete_ids: list[int] = field(default_factory=list)


async def synchronize_commutes(
    *,
    store: CredentialStore,
    token_manager: TokenProvider,
    strava_client: ActivityClient,
    notifier: ActivityNotifier,
    after: int | None = None,
    dry_run: bool = False,
    recheck_non_matches: bool = False,
    verbose: bool = False,
) -> SyncResult:
    """Evaluate candidate rides and make each configured update at most once.

    By default candidates are limited to rides after the rule was created.
    ``after`` supports an owner-requested historical backfill.  A dry run
    reads Strava only: it does not update Strava or write local processing
    state. ``recheck_non_matches`` re-evaluates activities that a prior run
    stored as non-matches, and ``verbose`` logs each decision without secrets.
    """

    result = SyncResult()
    for account in store.list_accounts():
        configuration = store.get_commute_configuration(account.athlete.id)
        if configuration is None:
            result.unconfigured_athlete_ids.append(account.athlete.id)
            continue

        validate_commute_configuration(configuration)
        cumulative_savings_cents = configuration.cumulative_savings_cents
        cumulative_co2_avoided_grams = configuration.cumulative_co2_avoided_grams
        access_token = await token_manager.get_access_token(account.athlete.id)
        summaries = await strava_client.list_athlete_activities(
            access_token,
            after=after if after is not None else configuration.created_at,
            per_page=100,
        )
        for summary in sorted(summaries, key=_activity_sort_key):
            activity_id = _activity_id(summary)
            if activity_id is None:
                continue
            previous = store.get_activity_processing(account.athlete.id, activity_id)
            if previous is not None and previous.status != "pending":
                if previous.status == "not_commute" and (
                    recheck_non_matches or _summary_is_strava_commute(summary)
                ):
                    previous = None
                else:
                    if verbose:
                        logger.info("activity=%s skipped: previously %s", activity_id, previous.status)
                    continue

            payload = await strava_client.get_activity(access_token, activity_id)
            activity = _activity_from_payload(payload)
            if activity.id != activity_id:
                raise CommuteConfigurationError("Strava returned an activity with an unexpected identifier")

            reservation = previous
            if reservation is None:
                decision = commute_decision(activity, configuration)
                if verbose:
                    if decision.matches:
                        logger.info("activity=%s matches: %s", activity_id, decision.reason)
                    else:
                        logger.info(
                            "activity=%s date=%s type=%s distance=%s does not match: %s",
                            activity_id,
                            activity.activity_date or "unknown",
                            activity.sport_type or "unknown",
                            _format_activity_distance(activity.distance_m),
                            decision.reason,
                        )
                if not decision.matches:
                    if not dry_run:
                        store.mark_activity_not_commute(account.athlete.id, activity_id)
                    result.non_matching_activity_ids.append(activity_id)
                    continue
                if dry_run:
                    result.would_update_activity_ids.append(activity_id)
                    continue
                savings_cents = calculate_savings_cents(configuration, activity.distance_m)
                activity_cumulative_savings_cents = cumulative_savings_cents + savings_cents
                co2_avoided_grams = calculate_co2_avoided_grams(configuration, activity.distance_m)
                activity_cumulative_co2_avoided_grams = cumulative_co2_avoided_grams + co2_avoided_grams
            elif verbose:
                logger.info("activity=%s matches: retrying a pending Commuter update", activity_id)

            if reservation is not None:
                if (
                    reservation.savings_cents is None
                    or reservation.cumulative_savings_cents is None
                    or reservation.co2_avoided_grams is None
                    or reservation.cumulative_co2_avoided_grams is None
                ):
                    raise CommuteConfigurationError("Stored commute processing state is invalid")
                savings_cents = reservation.savings_cents
                activity_cumulative_savings_cents = reservation.cumulative_savings_cents
                co2_avoided_grams = reservation.co2_avoided_grams
                activity_cumulative_co2_avoided_grams = reservation.cumulative_co2_avoided_grams

            if dry_run:
                result.would_update_activity_ids.append(activity_id)
                continue

            update: dict[str, object] = {
                "commute": True,
                "hide_from_home": True,
                "description": merge_commuter_block(
                    description=activity.description,
                    configuration=configuration,
                    distance_m=activity.distance_m,
                    savings_cents=savings_cents,
                    cumulative_savings_cents=activity_cumulative_savings_cents,
                    co2_avoided_grams=co2_avoided_grams,
                    cumulative_co2_avoided_grams=activity_cumulative_co2_avoided_grams,
                ),
            }
            await strava_client.update_activity(access_token, activity_id, update)
            await notifier.activity_updated(
                activity_id=activity_id,
                estimated_savings=_format_currency(savings_cents, configuration.currency),
                cumulative_savings=_format_currency(activity_cumulative_savings_cents, configuration.currency),
                co2_avoided=_format_co2_avoided(co2_avoided_grams),
                cumulative_co2_avoided=_format_co2_avoided(activity_cumulative_co2_avoided_grams),
            )
            if reservation is None:
                reservation = store.reserve_commute_activity(
                    athlete_id=account.athlete.id,
                    activity_id=activity_id,
                    savings_cents=savings_cents,
                    co2_avoided_grams=co2_avoided_grams,
                )
                if (
                    reservation.cumulative_savings_cents != activity_cumulative_savings_cents
                    or reservation.cumulative_co2_avoided_grams != activity_cumulative_co2_avoided_grams
                ):
                    raise CommuteConfigurationError("Commute savings total changed during synchronization")
            store.mark_activity_completed(account.athlete.id, activity_id)
            cumulative_savings_cents = activity_cumulative_savings_cents
            cumulative_co2_avoided_grams = activity_cumulative_co2_avoided_grams
            result.updated_activity_ids.append(activity_id)

    return result


def validate_commute_configuration(configuration: CommuteConfiguration) -> None:
    """Reject incomplete or unsafe manual settings before a live update."""

    if len(configuration.locations) < 2:
        raise CommuteConfigurationError("At least two locations are required")
    names = [location.name for location in configuration.locations]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise CommuteConfigurationError("Each location must have a unique, non-empty name")
    for location in configuration.locations:
        coordinate = location.coordinate
        if not -90 <= coordinate.latitude <= 90 or not -180 <= coordinate.longitude <= 180:
            raise CommuteConfigurationError("Location coordinates must be valid latitude/longitude values")
    if configuration.radius_m <= 0:
        raise CommuteConfigurationError("Location radius must be greater than zero")
    if configuration.combined_mpg <= 0:
        raise CommuteConfigurationError("Combined MPG must be greater than zero")
    if configuration.gas_price_cents < 0:
        raise CommuteConfigurationError("Gas price cannot be negative")
    if not configuration.currency:
        raise CommuteConfigurationError("A currency code is required")


def matches_commute(activity: ActivityDetails, configuration: CommuteConfiguration) -> bool:
    """Return whether a Ride connects Home and Work in either direction."""

    return commute_decision(activity, configuration).matches


def commute_decision(activity: ActivityDetails, configuration: CommuteConfiguration) -> CommuteDecision:
    """Explain whether a Ride is a configured or manually tagged commute."""

    if activity.sport_type != "Ride":
        return CommuteDecision(False, f"sport_type is {activity.sport_type!r}, not 'Ride'")
    if activity.distance_m is None:
        return CommuteDecision(False, "activity distance is unavailable")
    if activity.commute:
        return CommuteDecision(True, "already marked as a Strava commute")
    if activity.start is None and activity.end is None:
        return CommuteDecision(False, "start and end coordinates are unavailable")
    if activity.start is None:
        return CommuteDecision(False, "start coordinates are unavailable")
    if activity.end is None:
        return CommuteDecision(False, "end coordinates are unavailable")

    for origin, destination in _distinct_location_pairs(configuration.locations):
        start_m = haversine_meters(activity.start, origin.coordinate)
        end_m = haversine_meters(activity.end, destination.coordinate)
        if start_m <= configuration.radius_m and end_m <= configuration.radius_m:
            return CommuteDecision(
                True,
                f"{origin.name} -> {destination.name} (start-{origin.name}={start_m:.0f}m, "
                f"end-{destination.name}={end_m:.0f}m)",
            )
    return CommuteDecision(
        False,
        f"endpoints do not connect any two configured locations (radius={configuration.radius_m}m)",
    )


def _distinct_location_pairs(locations: tuple[Location, ...]) -> list[tuple[Location, Location]]:
    """Return every ordered pair of two different configured locations."""

    return [(origin, destination) for origin in locations for destination in locations if origin.name != destination.name]


def calculate_savings_cents(configuration: CommuteConfiguration, distance_m: float) -> int:
    """Calculate avoided fuel cost from an activity's recorded distance."""

    gallons = _fuel_avoided_gallons(configuration, distance_m)
    cents = gallons * Decimal(configuration.gas_price_cents)
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_co2_avoided_grams(configuration: CommuteConfiguration, distance_m: float) -> int:
    """Calculate avoided gasoline tailpipe CO₂ emissions in whole grams."""

    grams = _fuel_avoided_gallons(configuration, distance_m) * Decimal(GASOLINE_CO2_GRAMS_PER_GALLON)
    return int(grams.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def merge_commuter_block(
    *,
    description: str | None,
    configuration: CommuteConfiguration,
    distance_m: float,
    savings_cents: int,
    cumulative_savings_cents: int,
    co2_avoided_grams: int,
    cumulative_co2_avoided_grams: int,
) -> str:
    """Replace only Commuter's managed description block, preserving other text."""

    fuel_avoided = _fuel_avoided_gallons(configuration, distance_m)
    block = "\n".join(
        (
            "--- Commuter ---",
            f"Fuel avoided: {fuel_avoided.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f} gal",
            f"CO₂ avoided: {_format_co2_avoided(co2_avoided_grams)}",
            f"Estimated fuel savings: {_format_currency(savings_cents, configuration.currency)}",
            f"Cumulative fuel savings: {_format_currency(cumulative_savings_cents, configuration.currency)}",
            f"Cumulative CO₂ avoided: {_format_co2_avoided(cumulative_co2_avoided_grams)}",
            "--- /Commuter ---",
        )
    )
    if not description:
        return block
    if MANAGED_BLOCK_PATTERN.search(description):
        return MANAGED_BLOCK_PATTERN.sub(block, description)
    return f"{description.rstrip()}\n\n{block}"


def _activity_sort_key(summary: dict[str, object]) -> str:
    value = summary.get("start_date")
    return value if isinstance(value, str) else ""


def _activity_id(summary: dict[str, object]) -> int | None:
    value = summary.get("id")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _activity_from_payload(payload: dict[str, object]) -> ActivityDetails:
    activity_id = _activity_id(payload)
    if activity_id is None:
        raise CommuteConfigurationError("Strava activity has no valid identifier")
    sport_type = payload.get("sport_type")
    if not isinstance(sport_type, str):
        sport_type = payload.get("type") if isinstance(payload.get("type"), str) else None
    description = payload.get("description")
    return ActivityDetails(
        id=activity_id,
        sport_type=sport_type,
        start=_coordinate_from_value(payload.get("start_latlng")),
        end=_coordinate_from_value(payload.get("end_latlng")),
        description=description if isinstance(description, str) else None,
        commute=payload.get("commute") is True,
        activity_date=_activity_date(payload),
        distance_m=_distance_meters(payload.get("distance")),
    )


def _summary_is_strava_commute(summary: dict[str, object]) -> bool:
    return summary.get("commute") is True


def _activity_date(payload: dict[str, object]) -> str | None:
    value = payload.get("start_date_local")
    if not isinstance(value, str):
        value = payload.get("start_date")
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _distance_meters(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        distance_m = float(value)
    except (TypeError, ValueError):
        return None
    return distance_m if distance_m >= 0 else None


def _format_activity_distance(distance_m: float | None) -> str:
    if distance_m is None:
        return "unknown"
    return f"{distance_m / 1609.344:.2f}mi"


def _miles_from_meters(distance_m: float) -> Decimal:
    """Convert Strava's meter distance to miles without binary rounding artifacts."""

    return Decimal(str(distance_m)) / METERS_PER_MILE


def _fuel_avoided_gallons(configuration: CommuteConfiguration, distance_m: float) -> Decimal:
    """Return avoided gasoline based on an activity's distance and configured MPG."""

    return _miles_from_meters(distance_m) / Decimal(str(configuration.combined_mpg))


def _format_miles(miles: Decimal) -> str:
    """Render a ride distance to two decimal places without unnecessary zeroes."""

    rounded = miles.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded, "f").rstrip("0").rstrip(".")


def _format_co2_avoided(grams: int) -> str:
    """Render whole grams of avoided CO₂ as kilograms for an activity description."""

    kilograms = (Decimal(grams) / Decimal(1_000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{kilograms:.2f} kg"


def _coordinate_from_value(value: Any) -> Coordinate | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    latitude, longitude = value
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        coordinate = Coordinate(latitude=float(latitude), longitude=float(longitude))
    except (TypeError, ValueError):
        return None
    if not -90 <= coordinate.latitude <= 90 or not -180 <= coordinate.longitude <= 180:
        return None
    return coordinate


def haversine_meters(origin: Coordinate, destination: Coordinate) -> float:
    """Return great-circle distance in meters between two latitude/longitude points."""

    latitude_delta = math.radians(destination.latitude - origin.latitude)
    longitude_delta = math.radians(destination.longitude - origin.longitude)
    latitude_origin = math.radians(origin.latitude)
    latitude_destination = math.radians(destination.latitude)
    a = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_origin) * math.cos(latitude_destination) * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _format_currency(cents: int, currency: str) -> str:
    amount = Decimal(cents) / Decimal(100)
    if currency.upper() == "USD":
        return f"${amount:.2f}"
    return f"{currency.upper()} {amount:.2f}"


__all__ = [
    "CommuteConfiguration",
    "Coordinate",
    "SyncResult",
    "calculate_co2_avoided_grams",
    "calculate_savings_cents",
    "haversine_meters",
    "matches_commute",
    "merge_commuter_block",
    "synchronize_commutes",
    "validate_commute_configuration",
]
