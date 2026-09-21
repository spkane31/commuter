"""One-time conversion of legacy Fernet-encrypted Commuter data."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class LegacyMigrationError(ValueError):
    """Raised when legacy encrypted state cannot be converted safely."""


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Counts of records converted from the retired encrypted format."""

    accounts: int
    commute_configurations: int


def migrate_legacy_encrypted_storage(database_path: Path, encryption_key_path: Path) -> LegacyMigrationResult:
    """Convert a Fernet-encrypted database to the owner-only plaintext format.

    ``cryptography`` is imported only for this transition so it is not a normal
    runtime dependency. The key is removed only after every database update is
    committed successfully.
    """

    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:
        raise LegacyMigrationError(
            "Legacy migration requires temporary cryptography; run it with "
            "`uv run --with 'cryptography<46' commuter migrate-plaintext-storage` "
            "on a supported computer"
        ) from exc

    if not database_path.is_file():
        raise LegacyMigrationError(f"No database exists at {database_path}")
    if not encryption_key_path.is_file():
        raise LegacyMigrationError(f"No legacy encryption key exists at {encryption_key_path}")

    try:
        cipher = Fernet(encryption_key_path.read_bytes().strip())
    except (TypeError, ValueError) as exc:
        raise LegacyMigrationError(f"The legacy encryption key at {encryption_key_path} is invalid") from exc

    connection = sqlite3.connect(database_path)
    try:
        account_rows = connection.execute("SELECT athlete_id, access_token, refresh_token FROM accounts").fetchall()
        configuration_rows = connection.execute(
            "SELECT athlete_id, configuration FROM commute_configurations"
        ).fetchall()
        if not account_rows and not configuration_rows:
            raise LegacyMigrationError("The database contains no encrypted Commuter records to migrate")

        try:
            with connection:
                for athlete_id, access_token, refresh_token in account_rows:
                    connection.execute(
                        "UPDATE accounts SET access_token = ?, refresh_token = ? WHERE athlete_id = ?",
                        (
                            _decrypt(cipher, access_token, athlete_id, "access token", InvalidToken),
                            _decrypt(cipher, refresh_token, athlete_id, "refresh token", InvalidToken),
                            athlete_id,
                        ),
                    )
                for athlete_id, configuration in configuration_rows:
                    plaintext = _decrypt(cipher, configuration, athlete_id, "commute configuration", InvalidToken)
                    json.loads(plaintext)
                    connection.execute(
                        "UPDATE commute_configurations SET configuration = ? WHERE athlete_id = ?",
                        (plaintext, athlete_id),
                    )
        except LegacyMigrationError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise LegacyMigrationError("Legacy encrypted Commuter data could not be converted") from exc
    finally:
        connection.close()

    try:
        encryption_key_path.unlink()
    except OSError as exc:
        raise LegacyMigrationError(
            "The database was converted, but the legacy encryption key could not be removed; remove it manually"
        ) from exc

    return LegacyMigrationResult(accounts=len(account_rows), commute_configurations=len(configuration_rows))


def _decrypt(cipher: object, value: object, athlete_id: int, label: str, invalid_token: type[Exception]) -> str:
    """Decrypt one legacy BLOB and provide a useful record-specific failure."""

    if not isinstance(value, bytes):
        raise LegacyMigrationError(
            f"Athlete {athlete_id}'s {label} is already plaintext; do not run the legacy migration again"
        )
    try:
        return cipher.decrypt(value).decode("utf-8")  # type: ignore[attr-defined]
    except (UnicodeDecodeError, invalid_token) as exc:
        raise LegacyMigrationError(f"Athlete {athlete_id}'s {label} cannot be decrypted with the legacy key") from exc
