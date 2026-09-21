from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from commuter.legacy_migration import migrate_legacy_encrypted_storage
from commuter.store import CredentialStore

Fernet = pytest.importorskip("cryptography.fernet").Fernet


def test_legacy_migration_converts_credentials_and_commute_configuration(tmp_path: Path) -> None:
    database_path = tmp_path / "commuter.db"
    key_path = tmp_path / ".commuter.key"
    key = Fernet.generate_key()
    cipher = Fernet(key)
    key_path.write_bytes(key)
    configuration = {
        "home": {"latitude": 39.781003858657165, "longitude": -105.02303718996976},
        "work": {"latitude": 39.74341292772691, "longitude": -104.9886192024491},
        "radius_m": 150,
        "combined_mpg": 25.0,
        "gas_price_cents": 434,
        "vehicle_name": "2016 Subaru Forester",
        "currency": "USD",
    }
    connection = sqlite3.connect(database_path)
    with connection:
        connection.executescript(
            """
            CREATE TABLE accounts (
                athlete_id INTEGER PRIMARY KEY,
                username TEXT,
                scopes TEXT NOT NULL,
                access_token BLOB NOT NULL,
                refresh_token BLOB NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE commute_configurations (
                athlete_id INTEGER PRIMARY KEY,
                configuration BLOB NOT NULL,
                cumulative_savings_cents INTEGER NOT NULL,
                cumulative_co2_avoided_grams INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                123,
                "commuter",
                '["activity:read_all","activity:write"]',
                cipher.encrypt(b"access-token"),
                cipher.encrypt(b"refresh-token"),
                2_000_000_000,
                1,
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO commute_configurations VALUES (?, ?, ?, ?, ?, ?)
            """,
            (123, cipher.encrypt(json.dumps(configuration).encode("utf-8")), 2345, 6789, 1, 1),
        )
    connection.close()

    result = migrate_legacy_encrypted_storage(database_path, key_path)

    assert result.accounts == 1
    assert result.commute_configurations == 1
    assert not key_path.exists()
    store = CredentialStore(database_path)
    account = store.get_account(123)
    commute_configuration = store.get_commute_configuration(123)
    assert account is not None
    assert account.access_token == "access-token"
    assert account.refresh_token == "refresh-token"
    assert commute_configuration is not None
    assert commute_configuration.cumulative_savings_cents == 2345
    assert commute_configuration.cumulative_co2_avoided_grams == 6789
