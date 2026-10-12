"""Cloudflare Queues pull consumer that starts Temporal workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from commuter.temporal_workflows import ProcessStravaEvent

logger = logging.getLogger(__name__)

TEMPORAL_TASK_QUEUE = "commuter-strava"
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_RETRY_DELAY_SECONDS = 15
DEFAULT_VISIBILITY_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class PulledQueueMessage:
    """One Cloudflare HTTP pull message and its current lease."""

    body: str
    message_id: str
    lease_id: str


class CloudflareQueueError(RuntimeError):
    """A Cloudflare Queue HTTP API request failed."""


class CloudflareQueueClient:
    """Small HTTP adapter for Cloudflare Queue pull and acknowledgement APIs."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        account_id: str,
        queue_id: str,
        api_token: str,
        visibility_timeout_ms: int = DEFAULT_VISIBILITY_TIMEOUT_MS,
        batch_size: int = 1,
    ) -> None:
        self._http = http
        self._base_url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/queues/{queue_id}/messages"
        )
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._visibility_timeout_ms = visibility_timeout_ms
        self._batch_size = batch_size

    async def pull(self) -> list[PulledQueueMessage]:
        response = await self._http.post(
            f"{self._base_url}/pull",
            headers=self._headers,
            json={
                "visibility_timeout_ms": self._visibility_timeout_ms,
                "batch_size": self._batch_size,
            },
        )
        result = self._checked_result(response)
        messages = result.get("messages", [])
        if not isinstance(messages, list):
            raise CloudflareQueueError(
                "Cloudflare Queue returned an invalid message list"
            )
        parsed: list[PulledQueueMessage] = []
        for message in messages:
            if not isinstance(message, dict) or not all(
                isinstance(message.get(key), str) for key in ("body", "id", "lease_id")
            ):
                raise CloudflareQueueError(
                    "Cloudflare Queue returned an invalid message"
                )
            parsed.append(
                PulledQueueMessage(
                    body=message["body"],
                    message_id=message["id"],
                    lease_id=message["lease_id"],
                )
            )
        return parsed

    async def settle(
        self, acknowledgements: list[str], retries: list[dict[str, Any]]
    ) -> None:
        response = await self._http.post(
            f"{self._base_url}/ack",
            headers=self._headers,
            json={
                "acks": [{"lease_id": lease_id} for lease_id in acknowledgements],
                "retries": retries,
            },
        )
        self._checked_result(response)

    @staticmethod
    def _checked_result(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise CloudflareQueueError(
                f"Cloudflare Queue returned invalid JSON (HTTP {response.status_code})"
            ) from error
        if (
            not response.is_success
            or not isinstance(payload, dict)
            or not payload.get("success")
        ):
            raise CloudflareQueueError(
                f"Cloudflare Queue request failed (HTTP {response.status_code})"
            )
        result = payload.get("result")
        return result if isinstance(result, dict) else {}


def validate_strava_event(value: Any) -> dict[str, Any]:
    """Validate the event envelope before creating a workflow execution."""

    if not isinstance(value, dict):
        raise TypeError("Strava event must be an object")
    if value.get("object_type") not in {"activity", "athlete"}:
        raise ValueError("Unsupported Strava object type")
    if value.get("aspect_type") not in {"create", "update", "delete"}:
        raise ValueError("Unsupported Strava event aspect")
    for key in ("object_id", "owner_id", "subscription_id", "event_time"):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ValueError(f"Invalid Strava event field: {key}")
    return value


def workflow_id_for_event(event: dict[str, Any]) -> str:
    """Derive a deterministic workflow ID so redeliveries cannot duplicate work."""

    event = validate_strava_event(event)
    return ":".join(
        (
            "strava",
            str(event["subscription_id"]),
            event["object_type"],
            str(event["object_id"]),
            event["aspect_type"],
            str(event["event_time"]),
        )
    )


async def dispatch_queue_messages(
    messages: list[PulledQueueMessage],
    queue: CloudflareQueueClient,
    temporal_client: Client,
    *,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> None:
    """Start workflows and ack only accepted or already-started executions."""

    acknowledgements: list[str] = []
    retries: list[dict[str, Any]] = []
    for message in messages:
        try:
            event = validate_strava_event(json.loads(message.body))
            await temporal_client.start_workflow(
                ProcessStravaEvent.run,
                event,
                id=workflow_id_for_event(event),
                task_queue=TEMPORAL_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            # A previous delivery may have started the workflow before its
            # message acknowledgement reached Cloudflare.
            acknowledgements.append(message.lease_id)
        except Exception as error:  # noqa: BLE001 - any start failure must leave the message retryable.
            logger.warning(
                "Could not start workflow for Cloudflare queue message %s (%s)",
                message.message_id,
                type(error).__name__,
            )
            retries.append(
                {
                    "lease_id": message.lease_id,
                    "delay_seconds": retry_delay_seconds,
                }
            )
        else:
            acknowledgements.append(message.lease_id)

    if acknowledgements or retries:
        await queue.settle(acknowledgements, retries)


async def run_cloudflare_queue_poller(temporal_client: Client) -> None:
    """Poll Cloudflare over outbound HTTPS and dispatch accepted events."""

    account_id = _required_environment("CLOUDFLARE_ACCOUNT_ID")
    queue_id = _required_environment("CLOUDFLARE_QUEUE_ID")
    api_token = _required_environment("CLOUDFLARE_API_TOKEN")
    poll_interval = _positive_int_environment(
        "CLOUDFLARE_QUEUE_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_SECONDS
    )
    retry_delay = _positive_int_environment(
        "CLOUDFLARE_QUEUE_RETRY_DELAY_S", DEFAULT_RETRY_DELAY_SECONDS
    )
    visibility_timeout = _positive_int_environment(
        "CLOUDFLARE_QUEUE_VISIBILITY_TIMEOUT_MS", DEFAULT_VISIBILITY_TIMEOUT_MS
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as http:
        queue = CloudflareQueueClient(
            http,
            account_id=account_id,
            queue_id=queue_id,
            api_token=api_token,
            visibility_timeout_ms=visibility_timeout,
        )
        while True:
            try:
                messages = await queue.pull()
                if messages:
                    await dispatch_queue_messages(
                        messages,
                        queue,
                        temporal_client,
                        retry_delay_seconds=retry_delay,
                    )
                else:
                    await asyncio.sleep(poll_interval)
            except Exception as error:  # noqa: BLE001 - polling must survive transient HTTP/client failures.
                if isinstance(error, CloudflareQueueError):
                    logger.warning("Cloudflare Queue polling failed: %s", error)
                else:
                    logger.warning(
                        "Cloudflare Queue polling failed (%s)", type(error).__name__
                    )
                await asyncio.sleep(poll_interval)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set for the Temporal queue worker")
    return value


def _positive_int_environment(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed
