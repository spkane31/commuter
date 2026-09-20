"""Typed values used by the local Strava commuter service."""

from __future__ import annotations

from dataclasses import dataclass

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
class CommuteConfiguration:
    """One athlete's Home-to-Work commuter rule and fuel assumptions."""

    athlete_id: int
    home: Coordinate
    work: Coordinate
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
