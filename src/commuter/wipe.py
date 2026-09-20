"""Deliberate local removal of Commuter state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from commuter.config import ConfigurationError, Settings
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError, StravaClient


class WipeError(RuntimeError):
    """Raised when a normal wipe cannot revoke Strava access safely."""


@dataclass(frozen=True)
class WipeResult:
    """The non-sensitive result of a completed local wipe."""

    revoked_connections: int
    forced_local_wipe: bool


async def wipe_local_state(
    settings: Settings,
    strava_client: StravaClient,
    *,
    force_local: bool = False,
) -> WipeResult:
    """Revoke stored connections, then remove the database, sidecars, and key.

    A normal wipe preserves local state if it cannot revoke Strava access. The
    explicit force option removes local state even when revocation is impossible.
    """

    database_path = settings.database_path
    key_path = settings.encryption_key_path
    accounts = []

    if database_path.exists():
        if not key_path.is_file():
            if not force_local:
                raise WipeError(
                    "Cannot revoke Strava access because the local encryption key is missing; "
                    "rerun with --force-local to remove local files only"
                )
        else:
            try:
                store = CredentialStore(database_path, settings.load_or_create_encryption_key())
                try:
                    accounts = store.list_accounts()
                finally:
                    store.close()
            except (ConfigurationError, OSError, ValueError) as exc:
                if not force_local:
                    raise WipeError(
                        "Cannot read local credentials to revoke Strava access; "
                        "rerun with --force-local to remove local files only"
                    ) from exc

    if not force_local:
        try:
            for account in accounts:
                await strava_client.revoke(account.refresh_token)
        except StravaAPIError as exc:
            raise WipeError(
                "Strava access was not revoked; local files were retained. "
                "Retry later or rerun with --force-local to remove local files only"
            ) from exc

    for path in _local_state_paths(database_path, key_path):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WipeError(f"Could not remove local state at {path}") from exc

    return WipeResult(revoked_connections=len(accounts) if not force_local else 0, forced_local_wipe=force_local)


def _local_state_paths(database_path: Path, key_path: Path) -> tuple[Path, Path, Path, Path]:
    """Return the exact non-recursive files that comprise local Commuter state."""

    return (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
        key_path,
    )
