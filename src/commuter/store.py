"""Owner-only local SQLite storage for Strava credentials."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from commuter.models import Account, ActivityProcessing, Athlete, CommuteConfiguration, Coordinate, Location, TokenSet


class CredentialStore:
    """Store OAuth credentials in an owner-only local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize_schema()
        os.chmod(database_path, 0o600)

    def save_account(self, athlete: Athlete, scopes: set[str], tokens: TokenSet) -> None:
        """Insert or replace an athlete's token set."""

        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO accounts (
                    athlete_id, username, scopes, access_token, refresh_token,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id) DO UPDATE SET
                    username = excluded.username,
                    scopes = excluded.scopes,
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    athlete.id,
                    athlete.username,
                    json.dumps(sorted(scopes)),
                    tokens.access_token,
                    tokens.refresh_token,
                    tokens.expires_at,
                    now,
                    now,
                ),
            )

    def get_account(self, athlete_id: int) -> Account | None:
        """Return an account with credentials for the caller."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT athlete_id, username, scopes, access_token, refresh_token, expires_at
                FROM accounts
                WHERE athlete_id = ?
                """,
                (athlete_id,),
            ).fetchone()

        if row is None:
            return None
        return Account(
            athlete=Athlete(id=row["athlete_id"], username=row["username"]),
            scopes=set(json.loads(row["scopes"])),
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=row["expires_at"],
        )

    def list_accounts(self) -> list[Account]:
        """Return every local account for revocation during an explicit wipe."""

        with self._lock:
            rows = self._connection.execute("SELECT athlete_id FROM accounts").fetchall()
        return [account for row in rows if (account := self.get_account(row["athlete_id"])) is not None]

    def delete_account(self, athlete_id: int) -> None:
        """Permanently delete a local athlete record and its credentials."""

        with self._lock, self._connection:
            self._connection.execute("DELETE FROM activity_processing WHERE athlete_id = ?", (athlete_id,))
            self._connection.execute("DELETE FROM commute_configurations WHERE athlete_id = ?", (athlete_id,))
            self._connection.execute("DELETE FROM accounts WHERE athlete_id = ?", (athlete_id,))

    def save_commute_configuration(self, configuration: CommuteConfiguration) -> None:
        """Save owner-entered commute settings without resetting the total."""

        now = int(time.time())
        payload = _serialize_commute_configuration(configuration)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT cumulative_savings_cents, cumulative_co2_avoided_grams, created_at "
                "FROM commute_configurations WHERE athlete_id = ?",
                (configuration.athlete_id,),
            ).fetchone()
            cumulative_savings_cents = (
                existing["cumulative_savings_cents"] if existing is not None else configuration.cumulative_savings_cents
            )
            cumulative_co2_avoided_grams = (
                existing["cumulative_co2_avoided_grams"]
                if existing is not None
                else configuration.cumulative_co2_avoided_grams
            )
            created_at = existing["created_at"] if existing is not None else now
            self._connection.execute(
                """
                INSERT INTO commute_configurations (
                    athlete_id, configuration, cumulative_savings_cents, cumulative_co2_avoided_grams,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id) DO UPDATE SET
                    configuration = excluded.configuration,
                    updated_at = excluded.updated_at
                """,
                (
                    configuration.athlete_id,
                    payload,
                    cumulative_savings_cents,
                    cumulative_co2_avoided_grams,
                    created_at,
                    now,
                ),
            )

    def get_commute_configuration(self, athlete_id: int) -> CommuteConfiguration | None:
        """Return an athlete's commute rule and cumulative total."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT configuration, cumulative_savings_cents, cumulative_co2_avoided_grams, created_at
                FROM commute_configurations
                WHERE athlete_id = ?
                """,
                (athlete_id,),
            ).fetchone()
        if row is None:
            return None
        return _deserialize_commute_configuration(
            athlete_id=athlete_id,
            value=row["configuration"],
            cumulative_savings_cents=row["cumulative_savings_cents"],
            cumulative_co2_avoided_grams=row["cumulative_co2_avoided_grams"],
            created_at=row["created_at"],
        )

    def get_activity_processing(self, athlete_id: int, activity_id: int) -> ActivityProcessing | None:
        """Return prior handling state for an activity, if it has been evaluated."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT status, savings_cents, cumulative_savings_cents,
                       co2_avoided_grams, cumulative_co2_avoided_grams
                FROM activity_processing
                WHERE athlete_id = ? AND activity_id = ?
                """,
                (athlete_id, activity_id),
            ).fetchone()
        if row is None:
            return None
        return ActivityProcessing(
            athlete_id=athlete_id,
            activity_id=activity_id,
            status=row["status"],
            savings_cents=row["savings_cents"],
            cumulative_savings_cents=row["cumulative_savings_cents"],
            co2_avoided_grams=row["co2_avoided_grams"],
            cumulative_co2_avoided_grams=row["cumulative_co2_avoided_grams"],
        )

    def reserve_commute_activity(
        self,
        athlete_id: int,
        activity_id: int,
        savings_cents: int,
        co2_avoided_grams: int,
    ) -> ActivityProcessing | None:
        """Record a stable savings and CO₂ total after remote updates succeed."""

        now = int(time.time())
        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT status, savings_cents, cumulative_savings_cents,
                       co2_avoided_grams, cumulative_co2_avoided_grams
                FROM activity_processing
                WHERE athlete_id = ? AND activity_id = ?
                """,
                (athlete_id, activity_id),
            ).fetchone()
            if existing is not None and existing["status"] == "pending":
                return ActivityProcessing(
                    athlete_id=athlete_id,
                    activity_id=activity_id,
                    status="pending",
                    savings_cents=existing["savings_cents"],
                    cumulative_savings_cents=existing["cumulative_savings_cents"],
                    co2_avoided_grams=existing["co2_avoided_grams"],
                    cumulative_co2_avoided_grams=existing["cumulative_co2_avoided_grams"],
                )

            if existing is not None and existing["status"] != "not_commute":
                return None

            configuration = self._connection.execute(
                """
                SELECT cumulative_savings_cents, cumulative_co2_avoided_grams
                FROM commute_configurations
                WHERE athlete_id = ?
                """,
                (athlete_id,),
            ).fetchone()
            if configuration is None:
                raise LookupError(f"No commute configuration exists for athlete {athlete_id}")

            cumulative_savings_cents = configuration["cumulative_savings_cents"] + savings_cents
            cumulative_co2_avoided_grams = configuration["cumulative_co2_avoided_grams"] + co2_avoided_grams
            self._connection.execute(
                """
                UPDATE commute_configurations
                SET cumulative_savings_cents = ?, cumulative_co2_avoided_grams = ?, updated_at = ?
                WHERE athlete_id = ?
                """,
                (cumulative_savings_cents, cumulative_co2_avoided_grams, now, athlete_id),
            )
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO activity_processing (
                        athlete_id, activity_id, status, savings_cents, cumulative_savings_cents,
                        co2_avoided_grams, cumulative_co2_avoided_grams, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        athlete_id,
                        activity_id,
                        savings_cents,
                        cumulative_savings_cents,
                        co2_avoided_grams,
                        cumulative_co2_avoided_grams,
                        now,
                        now,
                    ),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE activity_processing
                    SET status = 'pending', savings_cents = ?, cumulative_savings_cents = ?,
                        co2_avoided_grams = ?, cumulative_co2_avoided_grams = ?, updated_at = ?
                    WHERE athlete_id = ? AND activity_id = ? AND status = 'not_commute'
                    """,
                    (
                        savings_cents,
                        cumulative_savings_cents,
                        co2_avoided_grams,
                        cumulative_co2_avoided_grams,
                        now,
                        athlete_id,
                        activity_id,
                    ),
                )
        return ActivityProcessing(
            athlete_id=athlete_id,
            activity_id=activity_id,
            status="pending",
            savings_cents=savings_cents,
            cumulative_savings_cents=cumulative_savings_cents,
            co2_avoided_grams=co2_avoided_grams,
            cumulative_co2_avoided_grams=cumulative_co2_avoided_grams,
        )

    def mark_activity_completed(self, athlete_id: int, activity_id: int) -> None:
        """Mark a reserved commute update as visible in Strava."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE activity_processing
                SET status = 'completed', updated_at = ?
                WHERE athlete_id = ? AND activity_id = ? AND status = 'pending'
                """,
                (int(time.time()), athlete_id, activity_id),
            )

    def mark_activity_not_commute(self, athlete_id: int, activity_id: int) -> None:
        """Remember a non-match so a polling run does not fetch it again."""

        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO activity_processing (athlete_id, activity_id, status, created_at, updated_at)
                VALUES (?, ?, 'not_commute', ?, ?)
                ON CONFLICT(athlete_id, activity_id) DO NOTHING
                """,
                (athlete_id, activity_id, now, now),
            )

    def close(self) -> None:
        """Close the SQLite connection."""

        with self._lock:
            self._connection.close()

    def _initialize_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    athlete_id INTEGER PRIMARY KEY,
                    username TEXT,
                    scopes TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commute_configurations (
                    athlete_id INTEGER PRIMARY KEY,
                    configuration TEXT NOT NULL,
                    cumulative_savings_cents INTEGER NOT NULL,
                    cumulative_co2_avoided_grams INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_processing (
                    athlete_id INTEGER NOT NULL,
                    activity_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'not_commute')),
                    savings_cents INTEGER,
                    cumulative_savings_cents INTEGER,
                    co2_avoided_grams INTEGER,
                    cumulative_co2_avoided_grams INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (athlete_id, activity_id)
                )
                """
            )
            self._add_column_if_missing(
                "commute_configurations",
                "cumulative_co2_avoided_grams",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._add_column_if_missing("activity_processing", "co2_avoided_grams", "INTEGER")
            self._add_column_if_missing("activity_processing", "cumulative_co2_avoided_grams", "INTEGER")

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        """Apply the narrow additive schema migrations needed by this local database."""

        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def _serialize_commute_configuration(configuration: CommuteConfiguration) -> str:
    """Encode owner-entered settings for SQLite."""

    return json.dumps(
        {
            "locations": [
                {
                    "name": location.name,
                    "latitude": location.coordinate.latitude,
                    "longitude": location.coordinate.longitude,
                }
                for location in configuration.locations
            ],
            "radius_m": configuration.radius_m,
            "combined_mpg": configuration.combined_mpg,
            "gas_price_cents": configuration.gas_price_cents,
            "vehicle_name": configuration.vehicle_name,
            "currency": configuration.currency,
        },
        separators=(",", ":"),
    )


def _deserialize_commute_configuration(
    athlete_id: int,
    value: str,
    cumulative_savings_cents: int,
    cumulative_co2_avoided_grams: int,
    created_at: int,
) -> CommuteConfiguration:
    """Decode validated local configuration from SQLite."""

    try:
        payload = json.loads(value)
        locations = tuple(
            Location(
                name=str(location["name"]),
                coordinate=Coordinate(latitude=float(location["latitude"]), longitude=float(location["longitude"])),
            )
            for location in payload["locations"]
        )
        return CommuteConfiguration(
            athlete_id=athlete_id,
            locations=locations,
            radius_m=int(payload["radius_m"]),
            combined_mpg=float(payload["combined_mpg"]),
            gas_price_cents=int(payload["gas_price_cents"]),
            vehicle_name=str(payload["vehicle_name"]),
            currency=str(payload["currency"]),
            cumulative_savings_cents=int(cumulative_savings_cents),
            cumulative_co2_avoided_grams=int(cumulative_co2_avoided_grams),
            created_at=int(created_at),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Stored commute configuration is invalid") from exc
