"""Context-first activity pipeline, constructed once at application startup."""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from commuter.models import ActivityEnvelope, CommuteConfiguration, ZoneSettings

if TYPE_CHECKING:
    from commuter.commute import (
        ActivityClient,
        ActivityNotifier,
        SyncResult,
        TokenProvider,
    )
    from commuter.sheets import SheetsAdapter
    from commuter.state import SourceCache
    from commuter.store import CredentialStore


@dataclass
class ProcessingContext:
    store: CredentialStore | None = None
    token_manager: TokenProvider | None = None
    strava_client: ActivityClient | None = None
    notifier: ActivityNotifier | None = None
    sheets: SheetsAdapter | None = None
    cache: SourceCache | None = None
    configuration: CommuteConfiguration | None = None
    commute_result: SyncResult | None = None
    zones: tuple[ZoneSettings, ...] = ()
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    reporting_timezone: str = "America/Denver"
    dry_run: bool = False
    recheck_non_matches: bool = False
    verbose: bool = False
    commute_after: int | None = None
    max_gap_s: float | None = None
    active_processors: tuple[str, ...] | None = None
    current_stage: str = "load"


class ActivityProcessor(Protocol):
    name: str

    async def process(
        self, context: ProcessingContext, activity: ActivityEnvelope
    ) -> ActivityEnvelope: ...


class Pipeline:
    """Pass the updated envelope through registered processors in declaration order."""

    def __init__(self, processors: tuple[ActivityProcessor, ...]) -> None:
        self.processors = processors
        names = [p.name for p in processors]
        if len(names) != len(set(names)):
            raise ValueError("Processor names must be unique")

    async def process(
        self, context: ProcessingContext, activity: ActivityEnvelope
    ) -> ActivityEnvelope:
        for processor in self.processors:
            if (
                context.active_processors is not None
                and processor.name not in context.active_processors
            ):
                continue
            context.current_stage = processor.name
            activity = await processor.process(context, activity)
        return activity


def build_pipeline(selected: tuple[str, ...]) -> Pipeline:
    """Register all available processors at startup and select their ordered subset."""

    from commuter.commute import CommuteProcessor
    from commuter.training import TrainingSheetsProcessor

    registered = (CommuteProcessor(), TrainingSheetsProcessor())
    if (
        not selected
        or len(selected) != len(set(selected))
        or set(selected) - {p.name for p in registered}
    ):
        raise ValueError(
            "--processors must select commuter, sheets, or commuter,sheets"
        )
    return Pipeline(tuple(p for p in registered if p.name in selected))


def history_start(end: datetime, months: int = 2) -> datetime:
    if months <= 0:
        raise ValueError("History months must be positive")
    absolute_month = end.year * 12 + end.month - 1 - months
    year, zero_month = divmod(absolute_month, 12)
    month = zero_month + 1
    return end.replace(
        year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1])
    )


@dataclass
class TrainingSyncResult:
    exported: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: bool = False


async def synchronize_training(
    *,
    settings,
    store,
    token_manager,
    strava_client,
    sheets,
    cache,
    mode: str = "sync",
    selected: tuple[str, ...] = ("sheets",),
    activity_id: int | None = None,
    months: int = 2,
    max_activities: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    recheck_non_matches: bool = False,
    verbose: bool = False,
    commute_after: int | None = None,
    run_deadline: float | None = None,
) -> TrainingSyncResult:
    """Run bounded, resumable discovery and the startup-registered pipeline.

    Discovery checkpoints retain inputs only. Export checkpoints advance after
    all required raw rows and rebuilt summaries succeed. Cached recalculation
    performs no Strava requests and never selects the commuter mutation stage.
    """

    from commuter.commute import SyncResult, validate_commute_configuration
    from commuter.sheets import SheetsError
    from commuter.state import content_version
    from commuter.strava import StravaAPIError, StravaRateLimitError

    def describe_error(error):
        # Adapter exceptions contain controlled messages, never request/credential payloads.
        return (
            str(error)[:300]
            if isinstance(error, (SheetsError, StravaAPIError, ValueError))
            else type(error).__name__
        )

    result = TrainingSyncResult()
    pipeline = build_pipeline(selected)
    accounts = store.list_accounts()
    if len(accounts) != 1:
        raise ValueError("Connect exactly one Strava account before training sync")
    account = accounts[0]
    athlete = account.athlete.id
    workbook = sheets.workbook
    if max_activities is None:
        max_activities = (
            max(1, len(cache.activity_ids(athlete)))
            if mode == "recalculate"
            else settings.training_max_activities
        )
    if (
        max_activities <= 0
        or settings.training_recent_days <= 0
        or settings.training_time_budget_s < 40
    ):
        raise ValueError(
            "Training needs positive activity/lookback limits and at least 40 seconds of run budget"
        )
    if mode == "recalculate" and selected != ("sheets",):
        raise ValueError("Cached recalculation is Sheets-only")
    deadline = run_deadline or time.monotonic() + settings.training_time_budget_s
    # Leave time for summary and completion persistence.
    work_deadline = deadline - 25
    sheets.deadline = deadline
    strava_client.deadline = work_deadline
    now = now or datetime.now(timezone.utc)
    zones, overrides = await sheets.snapshot_settings()
    configuration = (
        store.get_commute_configuration(athlete) if "commuter" in selected else None
    )
    if "commuter" in selected and configuration:
        validate_commute_configuration(configuration)
    context = ProcessingContext(
        store=store,
        token_manager=token_manager,
        strava_client=strava_client,
        sheets=sheets,
        cache=cache,
        configuration=configuration,
        commute_result=SyncResult(),
        zones=zones,
        overrides=overrides,
        reporting_timezone=settings.reporting_timezone,
        max_gap_s=settings.training_max_gap_s,
        dry_run=dry_run,
        verbose=verbose,
        recheck_non_matches=recheck_non_matches,
        commute_after=commute_after,
    )
    if "commuter" in selected:
        from commuter.discord import DiscordNotifier

        context.notifier = DiscordNotifier(settings)
    state_name = (
        ("history" if months == 2 else f"history_{months}m")
        if mode == "backfill"
        else mode
    )
    progress = store.get_progress(athlete, workbook, state_name)
    if mode in {"refresh", "recalculate"}:
        identifiers = (
            [activity_id] if mode == "refresh" else cache.activity_ids(athlete)
        )
        if any(i is None or i <= 0 for i in identifiers):
            raise ValueError("A positive activity ID is required")
        if progress is None or progress.get("complete") or mode == "refresh":
            progress = {
                "ids": {str(i): {} for i in identifiers},
                "done": [],
                "discovered": True,
            }
    elif progress is None or (progress.get("complete") and mode != "backfill"):
        end = int(now.timestamp())
        local = now.astimezone(ZoneInfo(settings.reporting_timezone))
        history = store.get_progress(athlete, workbook, "history")
        after = (
            int(history_start(local, months).timestamp())
            if mode == "backfill"
            else int((now - timedelta(days=settings.training_recent_days)).timestamp())
        )
        if mode == "reconcile":
            after = (
                int(history["after"])
                if history
                else int(history_start(local, months).timestamp())
            )
        training_after = after
        if mode == "sync" and "commuter" in selected and configuration:
            after = min(
                after,
                commute_after
                if commute_after is not None
                else configuration.created_at,
            )
        progress = {
            "after": after,
            "training_after": training_after,
            "before": end,
            "page": 1,
            "ids": {},
            "done": [],
            "discovered": False,
        }
    if progress.get("complete") and mode == "backfill":
        return result
    from commuter.training import METHOD_VERSION

    snapshot = content_version(
        {
            "zones": [
                vars(z) | {"boundaries": [vars(b) for b in z.boundaries]} for z in zones
            ],
            "overrides": overrides,
            "timezone": settings.reporting_timezone,
            "max_gap_s": settings.training_max_gap_s,
            "method_version": METHOD_VERSION,
        }
    )
    if progress.get("settings_snapshot") not in (None, snapshot):
        progress["done"] = []
    progress["settings_snapshot"] = snapshot
    if mode == "sync":
        for state in store.training_states(athlete, workbook):
            if state["status"] in {"failed", "uploaded"}:
                progress["ids"].setdefault(str(state["activity_id"]), {})
    quota = store.get_progress(athlete, workbook, "quota") or {}
    if mode != "recalculate" and quota.get("retry_at", 0) > time.time():
        result.pending = True
        result.errors.append("Strava quota retry time has not arrived")
        return result
    token = (
        await token_manager.get_access_token(athlete) if mode != "recalculate" else None
    )

    def persist():
        if not dry_run:
            store.save_progress(athlete, workbook, state_name, progress)

    persist()
    try:
        while not progress["discovered"] and time.monotonic() < work_deadline:
            summaries = await strava_client.list_athlete_activities(
                token,
                after=max(0, progress["after"] - 1),
                before=progress["before"],
                page=progress["page"],
                per_page=100,
            )
            for summary in summaries:
                identifier = summary.get("id")
                if (
                    not isinstance(identifier, int)
                    or isinstance(identifier, bool)
                    or identifier <= 0
                ):
                    continue
                timestamp = datetime.fromisoformat(
                    str(summary["start_date"]).replace("Z", "+00:00")
                ).timestamp()
                if progress["after"] <= timestamp < progress["before"]:
                    progress["ids"][str(identifier)] = {
                        k: summary[k]
                        for k in ("id", "start_date", "commute")
                        if k in summary
                    }
            progress["page"] += 1
            progress["discovered"] = len(summaries) < 100
            persist()
    except StravaRateLimitError as exc:
        if not dry_run:
            store.save_progress(
                athlete,
                workbook,
                "quota",
                {"retry_at": time.time() + exc.retry_after_seconds},
            )
        result.pending = True
        result.errors.append(str(exc))
        return result
    if not progress["discovered"]:
        result.pending = True
        return result
    if mode == "reconcile":
        if not dry_run:
            await sheets.reconcile(
                set(progress["ids"]), progress["after"], progress["before"]
            )
            await sheets.finish("success", "Activity-ID reconciliation complete")
            progress["complete"] = True
            progress["completed_at"] = int(time.time())
            persist()
        return result
    uploaded = []
    attempted = 0
    done = set(progress["done"])
    # Finish older pending work before newly discovered IDs; chronological commute order.
    ids = sorted(
        progress["ids"],
        key=lambda i: (progress["ids"][i].get("start_date", ""), int(i)),
    )
    for key in ids:
        if key in done:
            continue
        if attempted >= max_activities or time.monotonic() >= work_deadline:
            break
        identifier = int(key)
        previous = store.get_training_state(athlete, identifier, workbook)
        if previous and previous["status"] == "removed":
            done.add(key)
            continue
        hint = progress["ids"][key]
        if mode == "sync" and hint.get("start_date") and "commuter" in selected:
            old = (
                datetime.fromisoformat(
                    hint["start_date"].replace("Z", "+00:00")
                ).timestamp()
                < progress["training_after"]
            )
            prior_commute = store.get_activity_processing(athlete, identifier)
            if (
                old
                and prior_commute
                and prior_commute.status == "completed"
                and not (previous and previous["status"] in {"failed", "uploaded"})
            ):
                done.add(key)
                continue
        attempted += 1
        try:
            context.current_stage = "load"
            if mode == "recalculate":
                cached = cache.load(athlete, identifier)
                if cached is None:
                    raise ValueError(
                        "Cached source is unavailable; use training-refresh"
                    )
                payload, streams = cached["detail"], cached["streams"]
                reported_zones = cached.get("reported_zones", {}).get("zones", [])
            else:
                cached = cache.load(athlete, identifier) if mode == "backfill" else None
                if cached:
                    payload, streams = cached["detail"], cached["streams"]
                else:
                    payload = await strava_client.get_activity(token, identifier)
                    streams = await strava_client.get_activity_streams(
                        token, identifier
                    )
                if payload.get("id") != identifier:
                    raise ValueError(
                        "Strava returned an unexpected activity identifier"
                    )
                if cached and "reported_zones" in cached:
                    zone_response = cached["reported_zones"]
                elif "sheets" in selected:
                    zone_response = await strava_client.get_activity_zones(
                        token, identifier
                    )
                else:
                    zone_response = {"zones": []}
                reported_zones = zone_response["zones"]
                if not dry_run:
                    cache.save(
                        athlete,
                        identifier,
                        {
                            "detail": payload,
                            "streams": streams,
                            "reported_zones": zone_response,
                        },
                    )
            active = selected
            if mode == "sync":
                timestamp = datetime.fromisoformat(
                    str(payload["start_date"]).replace("Z", "+00:00")
                ).timestamp()
                if timestamp < progress["training_after"] and not (
                    previous and previous["status"] != "completed"
                ):
                    active = tuple(p for p in selected if p != "sheets")
            context.active_processors = active
            commute_record = store.get_activity_processing(athlete, identifier)
            envelope = ActivityEnvelope(
                athlete,
                identifier,
                payload,
                streams,
                effective_commute=payload.get("commute") is True
                or bool(commute_record and commute_record.status == "completed"),
                source_version=content_version(
                    {
                        "detail": payload,
                        "streams": streams,
                        "reported_zones": reported_zones,
                    }
                ),
                reported_zones=reported_zones,
            )
            envelope = await pipeline.process(context, envelope)
            if "sheets" in active:
                result.exported.append(identifier)
                uploaded.append(
                    (
                        identifier,
                        envelope.source_version,
                        envelope.training["settings_snapshot"],
                    )
                )
                if not dry_run:
                    store.save_training_state(
                        athlete,
                        identifier,
                        workbook,
                        "uploaded",
                        envelope.source_version,
                        envelope.training["settings_snapshot"],
                    )
            else:
                done.add(key)
        except Exception as exc:
            error = f"activity={identifier} stage={context.current_stage}: {describe_error(exc)}"
            result.errors.append(error)
            if not dry_run:
                store.save_training_state(
                    athlete, identifier, workbook, "failed", error=error
                )
            if isinstance(exc, StravaRateLimitError):
                if not dry_run:
                    store.save_progress(
                        athlete,
                        workbook,
                        "quota",
                        {"retry_at": time.time() + exc.retry_after_seconds},
                    )
                break
    remaining = set(ids) - done - {str(i) for i, _, _ in uploaded}
    result.pending = bool(remaining or result.errors)
    if mode == "backfill" and not result.pending and not progress.get("verified"):
        # A second complete scan catches late uploads that shifted API page numbers.
        verification = progress.setdefault("verification", {"page": 1, "ids": []})
        try:
            while time.monotonic() < work_deadline:
                summaries = await strava_client.list_athlete_activities(
                    token,
                    after=max(0, progress["after"] - 1),
                    before=progress["before"],
                    page=verification["page"],
                    per_page=100,
                )
                for summary in summaries:
                    identifier = summary.get("id")
                    if (
                        isinstance(identifier, int)
                        and not isinstance(identifier, bool)
                        and identifier > 0
                    ):
                        timestamp = datetime.fromisoformat(
                            str(summary["start_date"]).replace("Z", "+00:00")
                        ).timestamp()
                        if (
                            progress["after"] <= timestamp < progress["before"]
                            and str(identifier) not in verification["ids"]
                        ):
                            verification["ids"].append(str(identifier))
                            if str(identifier) not in progress["ids"]:
                                progress["ids"][str(identifier)] = {
                                    "id": identifier,
                                    "start_date": summary["start_date"],
                                }
                verification["page"] += 1
                if len(summaries) < 100:
                    progress["verified"] = True
                    break
                persist()
            remaining = set(progress["ids"]) - done - {str(i) for i, _, _ in uploaded}
            result.pending = bool(remaining or not progress.get("verified"))
            if not dry_run and progress.get("verified"):
                await sheets.reconcile(
                    set(verification["ids"]), progress["after"], progress["before"]
                )
        except Exception as exc:
            result.pending = True
            result.errors.append(f"stage=history_verification: {describe_error(exc)}")
    if not dry_run:
        try:
            await sheets.finish(
                "partial" if result.pending else "success",
                f"exported={len(result.exported)}; pending={len(remaining)}; "
                + "; ".join(result.errors),
            )
            for identifier, source_version, snapshot in uploaded:
                store.save_training_state(
                    athlete, identifier, workbook, "completed", source_version, snapshot
                )
                done.add(str(identifier))
            progress["done"] = sorted(done)
            progress["complete"] = not result.pending
            if progress["complete"]:
                progress["completed_at"] = int(time.time())
            persist()
        except Exception as exc:
            result.pending = True
            result.errors.append(f"stage=summaries: {describe_error(exc)}")
    return result
