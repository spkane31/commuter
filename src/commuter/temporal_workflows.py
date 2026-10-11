"""Deterministic Temporal workflows for Strava notifications."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="processStravaEvent")
class ProcessStravaEvent:
    """Dispatch one Strava notification to the Commuter activity worker."""

    @workflow.run
    async def run(self, event: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            "process_strava_event",
            event,
            start_to_close_timeout=timedelta(minutes=6),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2,
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
            ),
        )
