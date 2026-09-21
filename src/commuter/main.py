"""Local server and administrative command entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import uvicorn

from commuter.app import create_app
from commuter.auth import TokenManager
from commuter.commute import CommuteConfigurationError, synchronize_commutes, validate_commute_configuration
from commuter.config import Settings
from commuter.discord import DiscordAPIError, DiscordNotifier
from commuter.legacy_migration import LegacyMigrationError, migrate_legacy_encrypted_storage
from commuter.models import CommuteConfiguration, Coordinate
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError, StravaClient
from commuter.wipe import WipeError, wipe_local_state


def main() -> None:
    """Run the local server or an explicit administrative command."""

    parser = argparse.ArgumentParser(prog="commuter")
    subcommands = parser.add_subparsers(dest="command")
    wipe_parser = subcommands.add_parser("wipe", help="Revoke Strava access and remove local Commuter data")
    wipe_parser.add_argument(
        "--force-local",
        action="store_true",
        help="Remove local files without revoking Strava access when Strava is unavailable",
    )
    migrate_parser = subcommands.add_parser(
        "migrate-plaintext-storage",
        help="Convert the retired encrypted local database to plaintext",
    )
    migrate_parser.add_argument(
        "--legacy-key-path",
        type=str,
        help="Path to the Fernet key used by the retired encrypted database",
    )
    configure_parser = subcommands.add_parser(
        "configure-commute",
        help="Save one Home-to-Work commute rule for the connected athlete",
    )
    configure_parser.add_argument("--home", required=True, metavar="LATITUDE,LONGITUDE")
    configure_parser.add_argument("--work", required=True, metavar="LATITUDE,LONGITUDE")
    configure_parser.add_argument("--radius-m", required=True, type=int)
    configure_parser.add_argument("--combined-mpg", required=True, type=float)
    configure_parser.add_argument("--gas-price", required=True, metavar="DOLLARS_PER_GALLON")
    configure_parser.add_argument("--vehicle", required=True)
    configure_parser.add_argument("--currency", default="USD")
    sync_parser = subcommands.add_parser("sync", help="Poll Strava and update newly configured commuter rides")
    sync_parser.add_argument(
        "--backfill-days",
        type=int,
        metavar="DAYS",
        help="Consider rides from the last DAYS days instead of only rides after the rule was saved",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matching rides without updating Strava or local processing state",
    )
    sync_parser.add_argument(
        "--recheck-non-matches",
        action="store_true",
        help="Re-evaluate activities previously stored as non-matches",
    )
    sync_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log the match or non-match reason for every evaluated activity",
    )
    arguments = parser.parse_args()

    if arguments.command == "wipe":
        settings = Settings.from_environment()
        try:
            result = asyncio.run(
                wipe_local_state(
                    settings=settings,
                    strava_client=StravaClient(settings),
                    force_local=arguments.force_local,
                )
            )
        except WipeError as exc:
            parser.error(str(exc))
        if result.forced_local_wipe:
            print("Local Commuter data removed without revoking Strava access.")
        else:
            print(f"Revoked {result.revoked_connections} Strava connection(s) and removed local Commuter data.")
        return

    if arguments.command == "migrate-plaintext-storage":
        settings = Settings.from_environment()
        key_path = arguments.legacy_key_path or os.environ.get(
            "COMMUTER_ENCRYPTION_KEY_PATH",
            str(settings.database_path.with_name(".commuter.key")),
        )
        try:
            result = migrate_legacy_encrypted_storage(settings.database_path, Path(key_path))
        except LegacyMigrationError as exc:
            parser.error(str(exc))
        print(
            "Converted legacy encrypted storage to plaintext: "
            f"accounts={result.accounts}, commute_configurations={result.commute_configurations}."
        )
        return

    if arguments.command == "configure-commute":
        _configure_commute(arguments, parser)
        return

    if arguments.command == "sync":
        _sync_commutes(arguments, parser)
        return

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


def _configure_commute(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Persist a single owner-supplied Home-to-Work configuration."""

    settings = Settings.from_environment()
    store = CredentialStore(settings.database_path)
    try:
        accounts = store.list_accounts()
        if len(accounts) != 1:
            parser.error("Connect exactly one Strava account before configuring commuter rides")
        configuration = CommuteConfiguration(
            athlete_id=accounts[0].athlete.id,
            home=_parse_coordinate(arguments.home),
            work=_parse_coordinate(arguments.work),
            radius_m=arguments.radius_m,
            combined_mpg=arguments.combined_mpg,
            gas_price_cents=_gas_price_cents(arguments.gas_price),
            vehicle_name=arguments.vehicle,
            currency=arguments.currency.upper(),
        )
        validate_commute_configuration(configuration)
        store.save_commute_configuration(configuration)
    except (CommuteConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    finally:
        store.close()
    print(f"Commute configuration saved for athlete {configuration.athlete_id}.")


def _sync_commutes(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run one owner-only commuter synchronization pass."""

    if arguments.backfill_days is not None and arguments.backfill_days <= 0:
        parser.error("--backfill-days must be greater than zero")
    after = (
        int(time.time()) - arguments.backfill_days * 24 * 60 * 60
        if arguments.backfill_days is not None
        else None
    )
    if arguments.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_environment()
    store = CredentialStore(settings.database_path)
    strava_client = StravaClient(settings)
    try:
        notifier = DiscordNotifier(settings)
        result = asyncio.run(
            synchronize_commutes(
                store=store,
                token_manager=TokenManager(store=store, strava_client=strava_client),
                strava_client=strava_client,
                notifier=notifier,
                after=after,
                dry_run=arguments.dry_run,
                recheck_non_matches=arguments.recheck_non_matches,
                verbose=arguments.verbose,
            )
        )
    except (CommuteConfigurationError, DiscordAPIError, StravaAPIError, ValueError) as exc:
        parser.error(str(exc))
    finally:
        store.close()
    if arguments.dry_run:
        matching = ",".join(str(activity_id) for activity_id in result.would_update_activity_ids) or "none"
        print(
            "Commuter dry run complete: "
            f"would_update={len(result.would_update_activity_ids)} ({matching}), "
            f"non_matching={len(result.non_matching_activity_ids)}, "
            f"unconfigured_accounts={len(result.unconfigured_athlete_ids)}."
        )
        return
    print(
        "Commuter sync complete: "
        f"updated={len(result.updated_activity_ids)}, "
        f"non_matching={len(result.non_matching_activity_ids)}, "
        f"unconfigured_accounts={len(result.unconfigured_athlete_ids)}."
    )


def _parse_coordinate(value: str) -> Coordinate:
    """Parse a LATITUDE,LONGITUDE command-line argument."""

    try:
        latitude, longitude = (component.strip() for component in value.split(",", maxsplit=1))
        return Coordinate(latitude=float(latitude), longitude=float(longitude))
    except (TypeError, ValueError) as exc:
        raise ValueError("Coordinates must use LATITUDE,LONGITUDE") from exc


def _gas_price_cents(value: str) -> int:
    """Convert a decimal dollars-per-gallon command-line value to integer cents."""

    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Gas price must be a decimal number") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("Gas price must be a non-negative decimal number")
    return int((price * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
