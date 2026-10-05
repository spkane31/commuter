"""Typed values used by the local Strava commuter service."""

from __future__ import annotations

from dataclasses import dataclass, field

GASOLINE_CO2_GRAMS_PER_GALLON = 8_887


@dataclass(frozen=True)
class Athlete:
    """The minimal Strava athlete identity required by this application."""

    id: int
    username: str | None


@dataclass(frozen=True)
class TokenSet:
    """Strava OAuth tokens returned by authorization or refresh operations."""

    access_token: str
    refresh_token: str
    expires_at: int
    athlete: Athlete | None


@dataclass(frozen=True)
class Account:
    """A connected athlete and their decrypted credentials."""

    athlete: Athlete
    scopes: set[str]
    access_token: str
    refresh_token: str
    expires_at: int


@dataclass(frozen=True)
class Coordinate:
    """A user-configured geographic coordinate."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class Location:
    """A named, owner-configured commute endpoint."""

    name: str
    coordinate: Coordinate


@dataclass(frozen=True)
class CommuteConfiguration:
    """One athlete's commuter rule and fuel assumptions.

    A Ride between any two distinct configured locations, in either
    direction, is treated as a commute.
    """

    athlete_id: int
    locations: tuple[Location, ...]
    radius_m: int
    combined_mpg: float
    gas_price_cents: int
    vehicle_name: str
    currency: str
    cumulative_savings_cents: int = 0
    cumulative_co2_avoided_grams: int = 0
    created_at: int = 0


@dataclass(frozen=True)
class ActivityProcessing:
    """Durable state for one activity evaluated by the commuter synchronizer."""

    athlete_id: int
    activity_id: int
    status: str
    savings_cents: int | None
    cumulative_savings_cents: int | None
    co2_avoided_grams: int | None
    cumulative_co2_avoided_grams: int | None


@dataclass
class ActivityEnvelope:
    """Source data and independently replaceable processor results for one workout."""

    athlete_id: int
    id: int
    source: dict[str, object]
    streams: dict[str, object] | None = None
    effective_commute: bool = False
    training: dict[str, object] = field(default_factory=dict)
    source_version: str = ""
    reported_zones: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class ZoneBoundary:
    name: str
    lower_bpm: float
    upper_bpm: float | None


@dataclass(frozen=True)
class ZoneSettings:
    sport: str
    effective_from: str
    version: str
    boundaries: tuple[ZoneBoundary, ...]
    confirmed: bool
    max_gap_s: float
    method_source: str
    retrospective: bool = False


@dataclass(frozen=True)
class RollingMeasurement:
    time_s: float
    heart_rate_bpm: float | None
    pace_s_km: float | None
    interval_s: float
    distance_m: float | None
    moving: bool | None


@dataclass(frozen=True)
class ZoneResult:
    seconds: dict[str, float]
    classified_s: float
    unknown_s: float
    pace_time_s: dict[str, float]
    pace_distance_m: dict[str, float]
