"""Temporal worker entry point and side-effecting Strava event activity."""

from __future__ import annotations

import os
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from commuter.temporal_workflows import ProcessStravaEvent

TASK_QUEUE = "commuter-strava"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set for the Temporal worker")
    return value


async def run_temporal_worker() -> None:
    """Connect to Temporal Cloud and poll the queue used by the webhook."""

    client = await Client.connect(
        _required_environment("TEMPORAL_ADDRESS"),
        namespace=_required_environment("TEMPORAL_NAMESPACE"),
        api_key=_required_environment("TEMPORAL_API_KEY"),
        tls=True,
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProcessStravaEvent],
        activities=[process_strava_event],
    )
    await worker.run()


@activity.defn(name="process_strava_event")
async def process_strava_event(event: dict[str, Any]) -> dict[str, Any]:
    """Refresh and process one notified activity using the existing pipeline."""

    from commuter.config import Settings
    from commuter.pipeline import synchronize_training
    from commuter.sheets import SheetsAdapter
    from commuter.state import SourceCache, process_lock
    from commuter.store import CredentialStore
    from commuter.strava import StravaClient
    from commuter.auth import TokenManager

    event = _validate_event(event)
    if event["object_type"] == "athlete":
        raise ApplicationError(
            "Strava athlete authorization events require owner review",
            non_retryable=True,
        )

    settings = Settings.from_environment()
    if not settings.sheets_enabled:
        raise ApplicationError(
            "Enable Google Sheets before processing Strava webhook activities",
            non_retryable=True,
        )

    if event["aspect_type"] == "delete":
        await _remove_deleted_activity(settings, event["owner_id"], event["object_id"])
        return {"activity_id": event["object_id"], "status": "removed"}

    store = CredentialStore(settings.database_path)
    try:
        accounts = store.list_accounts()
        if len(accounts) != 1 or accounts[0].athlete.id != event["owner_id"]:
            raise ApplicationError(
                "Strava event owner does not match the locally connected athlete",
                non_retryable=True,
            )
        adapter = SheetsAdapter(
            settings.spreadsheet_id,
            settings.google_credentials_path,
            activity_sheet_name=settings.activity_sheet_name,
            reporting_timezone=settings.reporting_timezone,
        )
        cache = SourceCache(settings.cache_directory)
        try:
            async with StravaClient(settings) as client:
                with process_lock(settings.database_path.with_suffix(".sync.lock")):
                    result = await synchronize_training(
                        settings=settings,
                        store=store,
                        token_manager=TokenManager(store, client),
                        strava_client=client,
                        sheets=adapter,
                        cache=cache,
                        mode="refresh",
                        selected=("commuter", "sheets"),
                        activity_id=event["object_id"],
                    )
            if result.errors or result.pending or event["object_id"] not in result.exported:
                raise RuntimeError(
                    "; ".join(result.errors)
                    or "Strava activity processing did not complete its Sheets export"
                )
        finally:
            await adapter.close()
    finally:
        store.close()
    return {"activity_id": event["object_id"], "status": "processed"}


async def _remove_deleted_activity(settings, athlete_id: int, activity_id: int) -> None:
    from commuter.sheets import SheetsAdapter
    from commuter.state import process_lock
    from commuter.store import CredentialStore

    if not settings.sheets_enabled:
        raise ApplicationError(
            "Enable Google Sheets before processing Strava webhook deletions",
            non_retryable=True,
        )
    store = CredentialStore(settings.database_path)
    adapter = SheetsAdapter(
        settings.spreadsheet_id,
        settings.google_credentials_path,
        activity_sheet_name=settings.activity_sheet_name,
        reporting_timezone=settings.reporting_timezone,
    )
    try:
        accounts = store.list_accounts()
        if len(accounts) != 1 or accounts[0].athlete.id != athlete_id:
            raise ApplicationError(
                "Strava event owner does not match the locally connected athlete",
                non_retryable=True,
            )
        with process_lock(settings.database_path.with_suffix(".sync.lock")):
            await adapter.reconcile(
                set(), 0, 0, confirmed_removal=str(activity_id)
            )
            await adapter.finish("success", "Strava activity deletion processed")
            store.save_training_state(
                athlete_id,
                activity_id,
                settings.spreadsheet_id,
                "removed",
            )
    finally:
        await adapter.close()
        store.close()


def _validate_event(value: Any) -> dict[str, Any]:
    """Validate the Cloudflare webhook envelope before it reaches app code."""

    if not isinstance(value, dict):
        raise ApplicationError("Strava event must be an object", non_retryable=True)
    if value.get("object_type") not in {"activity", "athlete"}:
        raise ApplicationError("Unsupported Strava object type", non_retryable=True)
    if value.get("aspect_type") not in {"create", "update", "delete"}:
        raise ApplicationError("Unsupported Strava event aspect", non_retryable=True)
    for key in ("object_id", "owner_id", "subscription_id", "event_time"):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ApplicationError(f"Invalid Strava event field: {key}", non_retryable=True)
    return value
