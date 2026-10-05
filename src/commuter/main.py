"""Local server and administrative command entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import uvicorn

from commuter.app import create_app
from commuter.auth import TokenManager
from commuter.commute import (
    CommuteConfigurationError,
    synchronize_commutes,
    validate_commute_configuration,
)
from commuter.config import Settings
from commuter.discord import DiscordAPIError, DiscordNotifier
from commuter.models import CommuteConfiguration, Coordinate, Location
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError, StravaClient
from commuter.wipe import WipeError, wipe_local_state


def main() -> None:
    """Run the local server or an explicit administrative command."""

    parser = argparse.ArgumentParser(prog="commuter")
    subcommands = parser.add_subparsers(dest="command")
    wipe_parser = subcommands.add_parser(
        "wipe", help="Revoke Strava access and remove local Commuter data"
    )
    wipe_parser.add_argument(
        "--force-local",
        action="store_true",
        help="Remove local files without revoking Strava access when Strava is unavailable",
    )
    configure_parser = subcommands.add_parser(
        "configure-commute",
        help="Save the connected athlete's commute locations and rule",
    )
    configure_parser.add_argument(
        "--location",
        dest="locations",
        action="append",
        required=True,
        metavar="NAME,LATITUDE,LONGITUDE",
        help="A named commute endpoint; repeat for each location (at least 2 required)",
    )
    configure_parser.add_argument("--radius-m", required=True, type=int)
    configure_parser.add_argument("--combined-mpg", required=True, type=float)
    configure_parser.add_argument(
        "--gas-price", required=True, metavar="DOLLARS_PER_GALLON"
    )
    configure_parser.add_argument("--vehicle", required=True)
    configure_parser.add_argument("--currency", default="USD")
    sync_parser = subcommands.add_parser(
        "sync", help="Poll Strava and update newly configured commuter rides"
    )
    sync_parser.add_argument(
        "--processors",
        help="Select commuter, sheets, or commuter,sheets (startup order is preserved)",
    )
    sync_parser.add_argument(
        "--max-activities", type=int, help="Limit training activities in this run"
    )
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
    for name in (
        "sheets-setup",
        "training-backfill",
        "training-recalculate",
        "training-refresh",
        "training-reconcile",
        "training-remove",
    ):
        command_parser = subcommands.add_parser(name)
        command_parser.add_argument("--dry-run", action="store_true")
        command_parser.add_argument("--verbose", action="store_true")
        command_parser.add_argument(
            "--max-activities",
            type=int,
            help=(
                "Limit activities; recalculation processes all cached data if omitted"
                if name == "training-recalculate"
                else "Limit training activities in this run"
            ),
        )
        if name == "training-backfill":
            command_parser.add_argument("--months", type=int, default=2)
            command_parser.add_argument(
                "--resume",
                action="store_true",
                help="Resume the original captured range",
            )
        if name == "sheets-setup":
            command_parser.add_argument(
                "--refresh-charts",
                action="store_true",
                help="Recreate only managed charts after zone-column changes",
            )
        if name in {"training-refresh", "training-remove"}:
            command_parser.add_argument("--activity-id", type=int, required=True)
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
        except (WipeError, ValueError) as exc:
            parser.error(str(exc))
        if result.forced_local_wipe:
            print("Local Commuter data removed without revoking Strava access.")
        else:
            print(
                f"Revoked {result.revoked_connections} Strava connection(s) and removed local Commuter data."
            )
        return

    if arguments.command == "configure-commute":
        _configure_commute(arguments, parser)
        return

    if arguments.command == "sync":
        _sync_commutes(arguments, parser)
        return

    if arguments.command and (
        arguments.command.startswith("training-") or arguments.command == "sheets-setup"
    ):
        _training_command(arguments, parser)
        return

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


def _configure_commute(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Persist a single owner-supplied commute configuration."""

    settings = Settings.from_environment()
    store = CredentialStore(settings.database_path)
    try:
        accounts = store.list_accounts()
        if len(accounts) != 1:
            parser.error("Connect exactly one Strava account before configuring commuter rides")
        configuration = CommuteConfiguration(
            athlete_id=accounts[0].athlete.id,
            locations=tuple(_parse_location(value) for value in arguments.locations),
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


def _sync_commutes(
    arguments: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
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
    selected = (
        tuple(arguments.processors.split(","))
        if arguments.processors
        else (("commuter", "sheets") if settings.sheets_enabled else ("commuter",))
    )
    if selected != ("commuter",):
        _training_command(arguments, parser, settings=settings, selected=selected)
        return
    store = CredentialStore(settings.database_path)
    strava_client = StravaClient(settings)
    try:
        notifier = DiscordNotifier(settings)
        from commuter.state import process_lock

        with process_lock(settings.database_path.with_suffix(".sync.lock")):
            result = asyncio.run(
                synchronize_commutes(
                    store=store,
                    token_manager=TokenManager(
                        store=store, strava_client=strava_client
                    ),
                    strava_client=strava_client,
                    notifier=notifier,
                    after=after,
                    dry_run=arguments.dry_run,
                    recheck_non_matches=arguments.recheck_non_matches,
                    verbose=arguments.verbose,
                )
            )
    except (
        CommuteConfigurationError,
        DiscordAPIError,
        StravaAPIError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    finally:
        store.close()
    if arguments.dry_run:
        matching = (
            ",".join(
                str(activity_id) for activity_id in result.would_update_activity_ids
            )
            or "none"
        )
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


def _parse_location(value: str) -> Location:
    """Parse a NAME,LATITUDE,LONGITUDE command-line argument."""

    try:
        name, coordinate = value.split(",", maxsplit=1)
    except ValueError as exc:
        raise ValueError("Locations must use NAME,LATITUDE,LONGITUDE") from exc
    name = name.strip()
    if not name:
        raise ValueError("Locations must use NAME,LATITUDE,LONGITUDE")
    return Location(name=name, coordinate=_parse_coordinate(coordinate))


def _gas_price_cents(value: str) -> int:
    """Convert a decimal dollars-per-gallon command-line value to integer cents."""

    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Gas price must be a decimal number") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("Gas price must be a non-negative decimal number")
    return int((price * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _training_command(
    arguments, parser, *, settings=None, selected=("sheets",)
) -> None:
    from commuter.pipeline import build_pipeline, synchronize_training
    from commuter.sheets import SheetsAdapter, SheetsError
    from commuter.state import SourceCache, process_lock

    settings = settings or Settings.from_environment()
    store = None
    try:
        build_pipeline(selected)
        if "sheets" not in selected:
            raise ValueError("Training commands require the sheets processor")
        if (
            getattr(arguments, "backfill_days", None) is not None
            and arguments.backfill_days <= 0
        ):
            raise ValueError("--backfill-days must be positive")
        if (
            getattr(arguments, "months", 2) <= 0
            or getattr(arguments, "activity_id", 1) <= 0
        ):
            raise ValueError("History months and activity IDs must be positive")
        if arguments.verbose:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
        store = CredentialStore(settings.database_path)
        adapter = SheetsAdapter(
            settings.spreadsheet_id,
            settings.google_credentials_path,
            activity_sheet_name=settings.activity_sheet_name,
            reporting_timezone=settings.reporting_timezone,
        )
        cache = SourceCache(settings.cache_directory)

        async def run():
            try:
                if arguments.command == "sheets-setup":
                    if not arguments.dry_run:
                        await adapter.setup(refresh_charts=arguments.refresh_charts)
                    print(
                        "Workbook setup dry run: no changes made."
                        if arguments.dry_run
                        else "Workbook schema and initial charts installed."
                    )
                    return
                async with StravaClient(settings) as client:
                    deadline = time.monotonic() + settings.training_time_budget_s
                    mode = arguments.command.removeprefix("training-")
                    if mode == "remove":
                        accounts = store.list_accounts()
                        if len(accounts) != 1:
                            raise ValueError("Connect exactly one athlete")
                        if not arguments.dry_run:
                            await adapter.reconcile(
                                set(),
                                0,
                                0,
                                confirmed_removal=str(arguments.activity_id),
                            )
                            await adapter.finish(
                                "success", "Owner-confirmed reporting removal"
                            )
                            store.save_training_state(
                                accounts[0].athlete.id,
                                arguments.activity_id,
                                settings.spreadsheet_id,
                                "removed",
                            )
                        print(
                            "Would remove reporting activity."
                            if arguments.dry_run
                            else "Reporting activity removal complete."
                        )
                        return
                    after = (
                        int(time.time()) - arguments.backfill_days * 86400
                        if getattr(arguments, "backfill_days", None)
                        else None
                    )
                    async def batch(batch_deadline):
                        return await synchronize_training(
                            settings=settings,
                            store=store,
                            token_manager=TokenManager(store, client),
                            strava_client=client,
                            sheets=adapter,
                            cache=cache,
                            mode=mode,
                            selected=selected,
                            activity_id=getattr(arguments, "activity_id", None),
                            months=getattr(arguments, "months", 2),
                            max_activities=arguments.max_activities,
                            dry_run=arguments.dry_run,
                            recheck_non_matches=getattr(
                                arguments, "recheck_non_matches", False
                            ),
                            verbose=arguments.verbose,
                            commute_after=after,
                            run_deadline=batch_deadline,
                        )

                    result = await batch(deadline)
                    if mode == "sync" and not arguments.dry_run and not result.errors and not result.pending:
                        accounts = store.list_accounts()
                        athlete = accounts[0].athlete.id
                        history = store.get_progress(
                            athlete, settings.spreadsheet_id, "history"
                        )
                        reconcile = (
                            store.get_progress(
                                athlete, settings.spreadsheet_id, "reconcile"
                            )
                            or {}
                        )
                        followup = (
                            "backfill"
                            if history and not history.get("complete")
                            else "reconcile"
                            if history
                            and reconcile.get("completed_at", 0)
                            < time.time() - 7 * 86400
                            else None
                        )
                        if followup and time.monotonic() + 40 < deadline:
                            extra = await synchronize_training(
                                settings=settings,
                                store=store,
                                token_manager=TokenManager(store, client),
                                strava_client=client,
                                sheets=adapter,
                                cache=cache,
                                mode=followup,
                                selected=("sheets",),
                                max_activities=arguments.max_activities,
                                run_deadline=deadline,
                            )
                            result.exported.extend(extra.exported)
                            result.errors.extend(extra.errors)
                            result.pending = result.pending or extra.pending
                    print(
                        f"Training {'dry run' if arguments.dry_run else 'sync'}: exported={len(result.exported)}, pending={result.pending}, errors={len(result.errors)}."
                    )
                    for error in result.errors:
                        print(error)
                    if result.errors:
                        raise SystemExit(1)
                    if (
                        mode == "recalculate"
                        and arguments.max_activities is None
                        and not arguments.dry_run
                    ):
                        while result.pending and result.exported:
                            result = await batch(
                                time.monotonic() + settings.training_time_budget_s
                            )
                            print(
                                f"Training sync: exported={len(result.exported)}, pending={result.pending}, errors={len(result.errors)}."
                            )
                            for error in result.errors:
                                print(error)
                            if result.errors:
                                raise SystemExit(1)
            finally:
                await adapter.close()

        with process_lock(settings.database_path.with_suffix(".sync.lock")):
            asyncio.run(run())
    except (ValueError, OSError, SheetsError, StravaAPIError, DiscordAPIError) as exc:
        parser.error(str(exc))
    finally:
        if store:
            store.close()
